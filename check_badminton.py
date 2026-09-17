"""
Sledovač volných termínů badmintonu na baskalka.e-rezervace.cz
================================================================

Co dělá:
1. Přihlásí se přes Playwright (headless prohlížeč).
2. Přepne zobrazení rozvrhu na "Jeden den (vertikální)" - klasická
   tabulka s kurty jako sloupci a časy jako řádky.
3. Projde nejbližších HORIZONT_DNI dní, vybere jen úterky a středy.
4. Pro každý zajímavý den zkontroluje sloty 17-18, 18-19, 19-20 na
   všech kurtech - buňka je "volná", pokud na jejích souřadnicích
   NENÍ žádný barevný blok rezervace (div.event).
5. Pokud najde cokoliv volného, pošle souhrn na WhatsApp přes CallMeBot.

Známá omezení / co může být potřeba doladit:
- Klikání v kalendářovém popupu (funkce `nastav_datum`) předpokládá
  standardní RichFaces kalendář s tlačítky '<' a '>' pro měsíc.
  Pokud tahle část selže, pošli mi chybovou hlášku z GitHub Actions
  logu (Actions -> běh -> krok 'Spustit kontrolu') a doladíme to.
- Předpokládá se 13 kurtů (kurt 01-13). Pokud jich je jinak, uprav
  POCET_KURTU.
"""

import os
import sys
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import requests

# ---------------------------------------------------------------------------
# KONFIGURACE - z proměnných prostředí (GitHub Secrets)
# ---------------------------------------------------------------------------
USERNAME = os.environ["RESERVATION_USERNAME"]
PASSWORD = os.environ["RESERVATION_PASSWORD"]
CALLMEBOT_PHONE = os.environ["CALLMEBOT_PHONE"]
CALLMEBOT_APIKEY = os.environ["CALLMEBOT_APIKEY"]

LOGIN_URL = "http://baskalka.e-rezervace.cz/Branch/pages/WebLogin.faces"

# Dny v týdnu (Python: pondělí=0, úterý=1, středa=2, ...)
ZAJIMAVE_DNY_WEEKDAY = [1, 2]  # úterý, středa

# Hodiny začátku zajímavých slotů (17-18, 18-19, 19-20)
ZAJIMAVE_SLOTY_HODINY = [17, 18, 19]

HORIZONT_DNI = 14       # kolik dní dopředu kontrolovat
POCET_KURTU = 13        # kurt 01 až kurt 13

CZ_MESICE = [
    "leden", "únor", "březen", "duben", "květen", "červen",
    "červenec", "srpen", "září", "říjen", "listopad", "prosinec",
]


def poslat_whatsapp(zprava: str):
    url = "https://api.callmebot.com/whatsapp.php"
    params = {"phone": CALLMEBOT_PHONE, "text": zprava, "apikey": CALLMEBOT_APIKEY}
    r = requests.get(url, params=params, timeout=15)
    print(f"CallMeBot odpověď: {r.status_code} {r.text[:200]}")


def time_index(hour: int, minute: int = 0) -> int:
    """6:30 = index 0, každých 30 minut index +1 (viz id buněk sched_0_i_X_Y)."""
    minuty_od_pulnoci = hour * 60 + minute
    baze = 6 * 60 + 30
    return (minuty_od_pulnoci - baze) // 30


def cell_id(court_idx: int, t_idx: int) -> str:
    return f"sched_0_i_{court_idx}_{t_idx}"


def je_bunka_volna(page, court_idx: int, t_idx: int) -> bool:
    """Buňka je volná, pokud na ŽÁDNÉM z několika vzorkovaných bodů uvnitř
    (střed + 4 rohy s malým odsazením) není div.event. Vzorkování více
    bodů místo jednoho středu snižuje riziko chybného zásahu do mezery
    mezi sousedícími rezervačními bloky."""
    selector = f"#{cell_id(court_idx, t_idx)}"

    for pokus in range(3):
        try:
            locator = page.locator(selector)
            if locator.count() == 0:
                return False
            locator.scroll_into_view_if_needed()
            box = locator.bounding_box()
            if box is None:
                return False

            okraj = 4  # px odsazení od hrany buňky, ať netrefíme sousední blok
            x0, y0 = box["x"], box["y"]
            w, h = box["width"], box["height"]
            body = [
                (x0 + w / 2, y0 + h / 2),                    # střed
                (x0 + okraj, y0 + okraj),                    # levý horní roh
                (x0 + w - okraj, y0 + okraj),                # pravý horní roh
                (x0 + okraj, y0 + h - okraj),                # levý dolní roh
                (x0 + w - okraj, y0 + h - okraj),             # pravý dolní roh
            ]

            obsazeno = page.evaluate(
                """(body) => {
                    return body.some(([x, y]) => {
                        const el = document.elementFromPoint(x, y);
                        return el ? el.closest('.event') !== null : false;
                    });
                }""",
                body,
            )
            return not obsazeno
        except Exception as e:
            if pokus < 2:
                page.wait_for_timeout(500)
                continue
            print(f"Nepodařilo se přečíst buňku {selector} po 3 pokusech: {e}")
            return False
    return False


def zotav_z_chyby_serveru(page) -> bool:
    """Pokud se objevil dialog 'Nastala chyba' (např. 500 Internal Server
    Error), klikne na 'Obnovit rozvrh' a vrátí True. Jinak vrátí False."""
    dialog = page.locator("text='Nastala chyba'")
    if dialog.count() > 0 and dialog.first.is_visible():
        print("Zachycena chybová hláška serveru, klikám na 'Obnovit rozvrh'...")
        page.click("text='Obnovit rozvrh'")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        return True
    return False


def pockej_na_stabilni_rozvrh(page, max_cekani_ms=10000, interval_ms=500, potrebnych_stabilnich=3):
    """Rezervační bloky (div.event) se v tomto systému dokreslují
    asynchronně PO načtení samotné tabulky - pevná pauza proto nestačí
    a hrozí, že přečteme rozvrh dřív, než se rezervace stihnou objevit
    (falešně 'volno'). Při přepnutí data se navíc staré bloky nejdřív
    smažou (počet klesne na 0) a teprve pak se dotáhnou a vykreslí nové
    - kdybychom považovali za 'stabilní' už dvě stejná měření po sobě,
    hrozí, že chytíme právě tuhle prázdnou mezeru a vyhodnotíme obsazený
    rozvrh jako volný. Proto vyžadujeme stejný počet na N měření za sebou."""
    historie = []
    uplynulo = 0
    while uplynulo < max_cekani_ms:
        aktualni = page.locator("#resContainer .event").count()
        historie.append(aktualni)
        if len(historie) >= potrebnych_stabilnich and len(set(historie[-potrebnych_stabilnich:])) == 1:
            return
        page.wait_for_timeout(interval_ms)
        uplynulo += interval_ms


def nastav_zobrazeni_vertikalni(page):
    page.select_option(
        "#scheduleNavigForm\\:view_filter_menu",
        label="Jeden den (vertikální)",
    )
    page.wait_for_load_state("networkidle")


SCHEDULE_CALENDAR_SELECTOR = "#scheduleNavigForm\\:schedule_calendar"


def precti_zobrazeny_mesic_rok(page):
    text = page.locator(SCHEDULE_CALENDAR_SELECTOR).inner_text().lower()
    for i, nazev in enumerate(CZ_MESICE):
        if nazev in text:
            for cast in text.replace(",", " ").split():
                if cast.isdigit() and len(cast) == 4:
                    return int(cast), i + 1
    raise RuntimeError(f"Nepodařilo se rozpoznat měsíc/rok z kalendáře: {text}")


def nastav_datum(page, cilove_datum: datetime):
    cilovy_text = f"{cilove_datum.day}.{cilove_datum.month}.{cilove_datum.year}"
    aktualni_hodnota = page.input_value("#scheduleNavigForm\\:schedule_calendarInputDate")
    if aktualni_hodnota == cilovy_text:
        return  # už jsme na správném datu

    page.click("#scheduleNavigForm\\:schedule_calendarPopupButton")
    page.wait_for_selector(SCHEDULE_CALENDAR_SELECTOR, state="visible")

    for _ in range(12):  # pojistka proti nekonečné smyčce
        rok, mesic = precti_zobrazeny_mesic_rok(page)
        if (rok, mesic) == (cilove_datum.year, cilove_datum.month):
            break
        if (rok, mesic) < (cilove_datum.year, cilove_datum.month):
            page.click(f"{SCHEDULE_CALENDAR_SELECTOR} >> text='>'")
        else:
            page.click(f"{SCHEDULE_CALENDAR_SELECTOR} >> text='<'")
        page.wait_for_timeout(300)

    page.evaluate(
        """([selector, den]) => {
            const container = document.querySelector(selector);
            if (!container) return false;
            const walker = document.createTreeWalker(container, NodeFilter.SHOW_ELEMENT);
            let target = null;
            let node;
            while ((node = walker.nextNode())) {
                if (node.children.length === 0 && node.textContent.trim() === den) {
                    target = node;
                }
            }
            if (target) {
                target.click();
                return true;
            }
            return false;
        }""",
        [SCHEDULE_CALENDAR_SELECTOR, str(cilove_datum.day)],
    )
    page.wait_for_load_state("networkidle")


def najdi_volne_terminy():
    dnes = datetime.now()
    vysledky = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ))

        try:
            print(f"[{datetime.now()}] Přihlašuji se...")
            page.goto(LOGIN_URL, wait_until="load", timeout=60000)
            page.wait_for_selector("#username", state="visible", timeout=20000)
            page.fill("#username", USERNAME)
            page.fill("#password", PASSWORD)
            page.click("input[value='Přihlásit']")
            page.wait_for_load_state("networkidle")

            nastav_zobrazeni_vertikalni(page)
            pockej_na_stabilni_rozvrh(page)

            for posun in range(HORIZONT_DNI):
                datum = dnes + timedelta(days=posun)
                if datum.weekday() not in ZAJIMAVE_DNY_WEEKDAY:
                    continue

                print(f"[{datetime.now()}] Kontroluji {datum.strftime('%A %d.%m.%Y')}...")

                for pokus in range(3):
                    try:
                        nastav_datum(page, datum)
                        break
                    except PlaywrightTimeoutError:
                        if zotav_z_chyby_serveru(page):
                            print(f"Zkouším znovu nastavit datum (pokus {pokus + 2}/3)...")
                            continue
                        raise
                else:
                    print(f"Nepodařilo se nastavit datum {datum.strftime('%d.%m.%Y')} po 3 pokusech, přeskakuji.")
                    continue

                page.wait_for_timeout(500)
                pockej_na_stabilni_rozvrh(page)

                for hodina in ZAJIMAVE_SLOTY_HODINY:
                    t1 = time_index(hodina, 0)
                    t2 = time_index(hodina, 30)

                    for kurt_idx in range(POCET_KURTU):
                        if je_bunka_volna(page, kurt_idx, t1) and je_bunka_volna(page, kurt_idx, t2):
                            popis = (
                                f"{datum.strftime('%a %d.%m.')} {hodina}:00-{hodina + 1}:00, "
                                f"kurt {kurt_idx + 1:02d}"
                            )
                            vysledky.append(popis)
                            nazev_screenshotu = (
                                f"nalezeno_{datum.strftime('%Y%m%d')}"
                                f"_{hodina}h_kurt{kurt_idx + 1:02d}.png"
                            )
                            try:
                                page.screenshot(path=nazev_screenshotu)
                            except Exception as e:
                                print(f"Nepodařilo se uložit ověřovací screenshot: {e}")

                page.wait_for_timeout(800)  # šetrnější tempo vůči serveru
        except Exception:
            # Při jakékoliv chybě ulož screenshot a HTML aktuální stránky,
            # ať víme, co server doopravdy vrátil (debug.png / debug.html
            # se nahrají jako artifact v GitHub Actions).
            try:
                page.screenshot(path="debug.png", full_page=True)
                with open("debug.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                print("Uložen debug.png a debug.html pro diagnostiku.")
            except Exception as debug_e:
                print(f"Nepodařilo se uložit debug soubory: {debug_e}")
            raise
        finally:
            browser.close()

    return vysledky


def main():
    try:
        nalezene = najdi_volne_terminy()
    except Exception as e:
        print(f"Chyba při kontrole: {e}")
        sys.exit(1)

    if nalezene:
        zprava = "🏸 Volné termíny na badminton!\n" + "\n".join(nalezene)
        print(zprava)
        poslat_whatsapp(zprava)
    else:
        print(f"[{datetime.now()}] Žádné volné termíny v zajímavém okně.")


if __name__ == "__main__":
    main()
