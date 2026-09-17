"""
Sledovač volných termínů badmintonu na baskalka.e-rezervace.cz
================================================================

Jak to funguje:
1. Playwright otevře headless prohlížeč a přihlásí se pod tvým účtem.
2. Přejde na stránku s rezervacemi badmintonu.
3. Zkontroluje, jestli je v tobě zajímavém časovém okně volný kurt.
4. Pokud ano, pošle ti zprávu na WhatsApp přes CallMeBot.

DŮLEŽITÉ - musíš doplnit:
- Přihlašovací údaje (jako GitHub Secrets, viz. níže, NE natvrdo do kódu!)
- CSS/XPath selektory označené jako TODO (liší se web od webu, potřebuješ
  se podívat do Dev Tools na konkrétní stránce - návod je v komentářích).

Jak najít selektory (Dev Tools návod):
1. Otevři http://baskalka.e-rezervace.cz/Branch/pages/WebLogin.faces v Chrome/Firefoxu.
2. Klikni pravým tlačítkem na přihlašovací pole -> "Prozkoumat" (Inspect).
3. V zobrazeném HTML uvidíš atribut `id="..."` nebo `name="..."` daného inputu.
   To je selektor, který potřebuješ.
4. Totéž zopakuj pro heslo, přihlašovací tlačítko, a po přihlášení pro
   kalendář/tabulku volných termínů badmintonu.
"""

import os
import sys
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests

# ---------------------------------------------------------------------------
# KONFIGURACE - načítá se z proměnných prostředí (GitHub Secrets)
# ---------------------------------------------------------------------------
USERNAME = os.environ["RESERVATION_USERNAME"]
PASSWORD = os.environ["RESERVATION_PASSWORD"]
CALLMEBOT_PHONE = os.environ["CALLMEBOT_PHONE"]       # tvé číslo, format +420...
CALLMEBOT_APIKEY = os.environ["CALLMEBOT_APIKEY"]     # dostaneš při aktivaci CallMeBot

LOGIN_URL = "http://baskalka.e-rezervace.cz/Branch/pages/WebLogin.faces"

# Dny a hodinové sloty, které tě zajímají.
# Sloty jsou (začátek, konec) v 24h formátu - musí odpovídat formátu,
# jakým rezervační systém sloty zobrazuje (např. "17:00" nebo "17:00-18:00").
ZAJIMAVE_DNY = ["Tuesday", "Wednesday"]
ZAJIMAVE_SLOTY = [
    ("17:00", "18:00"),
    ("18:00", "19:00"),
    ("19:00", "20:00"),
]


def poslat_whatsapp(zprava: str):
    """Odešle zprávu přes CallMeBot API."""
    url = "https://api.callmebot.com/whatsapp.php"
    params = {
        "phone": CALLMEBOT_PHONE,
        "text": zprava,
        "apikey": CALLMEBOT_APIKEY,
    }
    r = requests.get(url, params=params, timeout=15)
    print(f"CallMeBot odpověď: {r.status_code} {r.text[:200]}")


def je_zajimavy_slot(text: str) -> bool:
    """
    Vrátí True, pokud text volného slotu (např. 'Úterý 17:00-18:00' nebo
    'Tuesday 17:00') odpovídá dni a hodině, které tě zajímají.

    TODO: Uprav podle skutečného formátu textu, jaký rezervační systém
    u volných slotů zobrazuje (den může být česky/anglicky/zkratkou,
    čas může mít jiný oddělovač apod.). Nejjednodušší je vypsat si
    `print(text)` pro pár slotů a podle toho parsování doladit.
    """
    text_lower = text.lower()

    # Mapování anglických názvů dnů na české varianty, které se mohou
    # v systému objevit - dopl' podle skutečnosti.
    dny_cz = {
        "Monday": ["pondělí", "po"],
        "Tuesday": ["úterý", "ut"],
        "Wednesday": ["středa", "st"],
        "Thursday": ["čtvrtek", "čt"],
        "Friday": ["pátek", "pá"],
    }

    den_sedi = any(
        any(varianta in text_lower for varianta in dny_cz.get(den, [den.lower()]))
        for den in ZAJIMAVE_DNY
    )
    if not den_sedi:
        return False

    cas_sedi = any(
        cas_od in text or cas_do in text
        for cas_od, cas_do in ZAJIMAVE_SLOTY
    )
    return cas_sedi


def zkontroluj_terminy():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print(f"[{datetime.now()}] Otevírám přihlašovací stránku...")
        page.goto(LOGIN_URL, wait_until="networkidle")

        # -------------------------------------------------------------
        # TODO: Doplň skutečné selektory z Dev Tools
        # -------------------------------------------------------------
        # Příklad - uprav podle skutečného id/name inputů na stránce:
        page.fill("#username", USERNAME)          # TODO: uprav selektor
        page.fill("#password", PASSWORD)           # TODO: uprav selektor
        page.click("#loginButton")                 # TODO: uprav selektor

        page.wait_for_load_state("networkidle")
        print(f"[{datetime.now()}] Přihlášení odesláno, čekám na načtení...")

        # -------------------------------------------------------------
        # TODO: Přejdi na stránku s rezervacemi badmintonu.
        # Buď je to přímý odkaz (page.goto("URL_BADMINTONU")),
        # nebo je potřeba proklikat menu (page.click("text=Badminton")).
        # -------------------------------------------------------------
        # page.click("text=Badminton")  # TODO: uprav podle skutečnosti
        # page.wait_for_load_state("networkidle")

        # -------------------------------------------------------------
        # TODO: Najdi volné termíny na stránce.
        # Rezervační systémy typicky označují volné sloty barvou/třídou
        # (např. class="free" vs class="occupied"). V Dev Tools zkontroluj,
        # jaký CSS selektor volné sloty odlišuje.
        # -------------------------------------------------------------
        volne_sloty = page.query_selector_all(".free-slot")  # TODO: uprav selektor

        nalezene = []
        for slot in volne_sloty:
            text = slot.inner_text().strip()
            if je_zajimavy_slot(text):
                nalezene.append(text)

        browser.close()
        return nalezene


def main():
    try:
        nalezene = zkontroluj_terminy()
    except Exception as e:
        print(f"Chyba při kontrole: {e}")
        sys.exit(1)

    if nalezene:
        zprava = "🏸 Volný termín na badminton!\n" + "\n".join(nalezene)
        print(zprava)
        poslat_whatsapp(zprava)
    else:
        print(f"[{datetime.now()}] Žádné volné termíny v zajímavém okně.")


if __name__ == "__main__":
    main()
