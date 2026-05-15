"""
03_synka_data.py — Daglig tillståndsbaserad synkronisering.

Flöde:
  1. Kontrollera latest_updated.json — om ny data: kor om 01_hamta_listor
  2. Ladda ned PDF:er för nya dokument av bulk-typ (förordningsmotiv,
     remissmissiv, internationella överenskommelser)
  3. Hämta nya regeringsbeslut via Filter/GetFilteredItems sedan senaste synk

Körning (typiskt via cron/launchd dagligen):
  python 03_synka_data.py

Tvingad körning (hämtar oavsett timestamp):
  python 03_synka_data.py --tvinga

Schemaläggning (körs via --installera-schema):
  python 03_synka_data.py --installera-schema
  Stöder cron (Linux/macOS) och launchd (macOS) — välj via SCHEMALAGGARE i .env.
"""
import os
import re
import time
import json
import logging
from datetime import datetime, date, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import db
import hamta_listor_lib as listor
from hamta_listor_lib import upsert_dokument
from pdf_lib import behandla_ett_dokument, pdf_cache_sokvag

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

REGERINGEN_BAS   = "https://www.regeringen.se"
FILTER_API_URL   = (
    f"{REGERINGEN_BAS}/Filter/GetFilteredItems"
    "?lang=sv&filterType=Structure&filterByType=CasePage"
    "&rootPageReference=548462&isInEditMode=&sortAlphabetically=False"
    "&filterFromToday=False&preFilteredCategories=&preFilteredBlockCategories="
    "&filteredContentCategories=&filteredPoliticalLevelCategories="
    "&filteredPoliticalAreaCategories=&filteredPublisherCategories="
    "&searchText=&searchQuery="
)

# Dokumenttyper som ska ha PDF:er laddade ned vid synk
BULK_TYPER = {"1326", "2099", "1332"}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; gov-dokument-mcp/1.0)",
    "Referer": REGERINGEN_BAS,
})


# ---------------------------------------------------------------------------
# Besluts-API
# ---------------------------------------------------------------------------

def hamta_beslut_sida(from_date: str, to_date: str, sida: int, sidstorlek: int = 100) -> dict:
    """Hämtar en sida med regeringsbeslut från Filter/GetFilteredItems."""
    params = {
        "fromDate": from_date,
        "toDate": to_date,
        "page": sida,
        "pageSize": sidstorlek,
        "displayLimited": "False",
    }
    svar = SESSION.get(FILTER_API_URL, params=params, timeout=15)
    svar.raise_for_status()
    return svar.json()


def tolka_beslut_html(html_strang: str) -> list[dict]:
    """
    Parsar HTML-strängen från Filter/GetFilteredItems och returnerar
    strukturerade beslutsposter.
    """
    soup = BeautifulSoup(html_strang, "lxml")
    beslut = []

    for li in soup.find_all("li"):
        post = {}

        # Titel (i <strong>)
        rubrik = li.find("strong")
        post["titel"] = rubrik.get_text(strip=True) if rubrik else None

        # Stycket med Regeringsärendenummer, Diarienummer, Chefstjänsteman
        stycken = li.find_all("p", class_="sortextended__excerpt")
        for stycke in stycken:
            text = stycke.get_text(" ", strip=True)

            m = re.search(r"Regeringsärendenummer:\s*([\w:]+)", text)
            if m:
                post["regeringsarendenummer"] = m.group(1)

            m = re.search(r"Diarienummer:\s*([^·]+)", text)
            if m:
                post["diarienummer_text"] = m.group(1).strip().rstrip(",")

            m = re.search(r"Ansvarig chefstjänsteman:\s*(.+?)(?:\s*·|$)", text)
            if m:
                post["ansvarig_chefstjansteman"] = m.group(1).strip()

        # Veckolänk, statsråd, departement
        tidsblock = li.find("div", class_="block--timeLinks")
        if tidsblock:
            lank = tidsblock.find("a")
            if lank:
                post["vecka_url"] = lank.get("href", "")

            # Statsråd och departement — texten efter länken
            p_text = tidsblock.get_text(" ", strip=True)
            # Ta bort länktexten
            lanktext = lank.get_text(strip=True) if lank else ""
            efter_lank = p_text.replace(lanktext, "", 1).strip().lstrip("·").strip()

            delar = [d.strip() for d in efter_lank.split(",") if d.strip()]
            if len(delar) >= 2:
                post["statsrad"] = delar[0]
                post["departement"] = delar[1]
            elif len(delar) == 1:
                post["statsrad"] = delar[0]

        if post.get("titel"):
            beslut.append(post)

    return beslut


def spara_beslut(beslutslista: list[dict], conn, use_postgres: bool) -> int:
    """Lagrar beslut i databasen. Returnerar antal nya poster."""
    cur = conn.cursor()
    tabell_b  = "gov_data.beslut" if use_postgres else "beslut"
    tabell_d  = "gov_data.beslut_diarienummer" if use_postgres else "beslut_diarienummer"
    plats     = "%s" if use_postgres else "?"
    nya = 0

    for b in beslutslista:
        # Kontrollera om beslutet redan finns (matcha på titel + vecka_url)
        cur.execute(
            f"SELECT id FROM {tabell_b} WHERE titel = {plats} AND vecka_url = {plats}",
            (b.get("titel"), b.get("vecka_url")),
        )
        if cur.fetchone():
            continue  # Redan indexerat

        # Parsa vecka och år ur vecka_url, t.ex. /...vecka-18-2026/
        vecka_url_val = b.get("vecka_url") or ""
        vecka_match   = re.search(r"vecka-(\d+)-(\d{4})", vecka_url_val)
        vecka_nummer  = int(vecka_match.group(1)) if vecka_match else None
        vecka_ar      = int(vecka_match.group(2)) if vecka_match else None

        cur.execute(f"""
            INSERT INTO {tabell_b}
                (titel, regeringsarendenummer, diarienummer_text,
                 ansvarig_chefstjansteman, vecka_url, statsrad, departement,
                 vecka_nummer, vecka_ar)
            VALUES ({','.join([plats]*9)})
            {'RETURNING id' if use_postgres else ''}
        """, (
            b.get("titel"),
            b.get("regeringsarendenummer"),
            b.get("diarienummer_text"),
            b.get("ansvarig_chefstjansteman"),
            vecka_url_val,
            b.get("statsrad"),
            b.get("departement"),
            vecka_nummer,
            vecka_ar,
        ))

        if use_postgres:
            beslut_id = cur.fetchone()[0]
        else:
            beslut_id = cur.lastrowid

        # Normaliserade diarienummer
        dnr_raw = b.get("diarienummer_text") or ""
        for dnr in [d.strip() for d in dnr_raw.split(",") if d.strip()]:
            komplett = bool(re.match(r"[A-Za-zÅÄÖåäö]+\d{4}/\d+", dnr))
            cur.execute(
                f"INSERT INTO {tabell_d} (beslut_id, diarienummer, komplett) VALUES ({plats},{plats},{plats})",
                (beslut_id, dnr, komplett),
            )

        nya += 1

    conn.commit()
    cur.close()
    return nya


def synka_beslut(conn, use_postgres: bool) -> int:
    """Hämtar alla nya beslut sedan senaste synk och lagrar dem."""
    senaste = db.hamta_synkstatus("beslut_senast_indexerat_datum")
    if senaste:
        from_date = senaste
    else:
        # Första körningen — hämta från API:ets startdatum
        from_date = "2024-09-25"

    to_date = date.today().isoformat()
    log.info(f"Hämtar beslut {from_date} → {to_date}")

    # Hämta forsta sidan för att få TotalCount
    forsta = hamta_beslut_sida(from_date, to_date, sida=1, sidstorlek=100)
    totalt = forsta.get("TotalCount", 0)
    log.info(f"  {totalt} beslut att hämta")

    if totalt == 0:
        db.spara_synkstatus("beslut_senast_indexerat_datum", to_date)
        return 0

    alla_beslut = tolka_beslut_html(forsta.get("Message", ""))
    sidor = (totalt + 99) // 100

    for sida in range(2, sidor + 1):
        svar = hamta_beslut_sida(from_date, to_date, sida=sida, sidstorlek=100)
        alla_beslut.extend(tolka_beslut_html(svar.get("Message", "")))
        time.sleep(0.3)

    nya = spara_beslut(alla_beslut, conn, use_postgres)
    db.spara_synkstatus("beslut_senast_indexerat_datum", to_date)
    log.info(f"  {nya} nya beslut indexerade (av {len(alla_beslut)} hämtade)")
    return nya


# ---------------------------------------------------------------------------
# PDF-synk för bulk-typer
# ---------------------------------------------------------------------------

def synka_nya_pdf(conn, use_postgres: bool) -> int:
    """Laddar ned PDF:er för nya bulk-dokument som saknar fulltext."""
    cur = conn.cursor()
    tabell = "gov_data.dokument" if use_postgres else "dokument"
    plats  = "%s" if use_postgres else "?"

    typ_lista = list(BULK_TYPER)
    if use_postgres:
        cur.execute(f"""
            SELECT id, url, typ_kod, titel, bilagor
            FROM {tabell}
            WHERE typ_kod = ANY(%s) AND fulltext_md IS NULL
            ORDER BY publicerad DESC NULLS LAST
        """, (typ_lista,))
    else:
        platser = ",".join(["?"] * len(typ_lista))
        cur.execute(f"""
            SELECT id, url, typ_kod, titel, bilagor
            FROM {tabell}
            WHERE typ_kod IN ({platser}) AND fulltext_md IS NULL
            ORDER BY publicerad DESC
        """, typ_lista)

    dokument = [
        {"id": r[0], "url": r[1], "typ_kod": r[2], "titel": r[3], "bilagor": r[4]}
        for r in cur.fetchall()
    ]
    cur.close()

    if not dokument:
        return 0

    log.info(f"  {len(dokument)} nya bulk-dokument att ladda ned")
    ok = 0
    for doc in dokument:
        status = behandla_ett_dokument(doc, conn, use_postgres)
        if status.startswith("OK"):
            ok += 1
        log.debug(status)
        time.sleep(0.5)

    return ok


# ---------------------------------------------------------------------------
# Huvudfunktion
# ---------------------------------------------------------------------------

def kor(tvinga: bool = False):
    db.initiera_schema()
    conn = db.get_conn()
    use_postgres = db.DATABASE_URL.startswith("postgresql")

    log.info("=== Daglig synk startar ===")

    # 1. g0v.se — metadata
    senaste_kand = db.hamta_synkstatus("g0v_latest_updated")
    aktuell = listor.hamta_senast_uppdaterad()
    if tvinga or senaste_kand != aktuell:
        log.info("Ny data på g0v.se — uppdaterar JSON-listor")
        listor_conn = db.get_conn()
        for endpoint, typ_kod in listor.LISTOR:
            poster = listor.hamta_lista(endpoint)
            upsert_dokument(poster, typ_kod, listor_conn, use_postgres)
            time.sleep(0.5)
        listor_conn.close()
        db.spara_synkstatus("g0v_latest_updated", aktuell)
        log.info("JSON-listor uppdaterade")
    else:
        log.info("Ingen ny data på g0v.se — hoppar över JSON-hämtning")

    # 2. PDF:er för nya bulk-dokument
    nya_pdf = synka_nya_pdf(conn, use_postgres)
    if nya_pdf:
        log.info(f"Laddade ned och indexerade {nya_pdf} nya PDF:er")

    # 3. Regeringsbeslut
    nya_beslut = synka_beslut(conn, use_postgres)

    conn.close()
    db.spara_synkstatus("senaste_synk", datetime.utcnow().isoformat())
    log.info(f"=== Synk klar — {nya_pdf} nya PDF:er, {nya_beslut} nya beslut ===")


def installera_schema(script_sokvag: str, python_sokvag: str):
    """Installerar schemalagt jobb baserat på SCHEMALAGGARE i .env.

    Stöder: cron (Linux och macOS) och launchd (macOS).
    """
    import platform
    import subprocess
    from pathlib import Path

    schemalaggare = os.getenv("SCHEMALAGGARE", "cron").lower()
    cron_schema   = os.getenv("CRON_SCHEMA", "45 6 * * *")
    script_abs    = str(Path(script_sokvag).resolve())

    if schemalaggare == "launchd":
        if platform.system() != "Darwin":
            log.error("launchd är bara tillgängligt på macOS. Byt till SCHEMALAGGARE=cron.")
            return

        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_fil = plist_dir / "se.riksdag-ai.gov-dokument-synk.plist"
        plist_dir.mkdir(parents=True, exist_ok=True)

        # Tolka timme och minut ur cron-schemat
        delar = cron_schema.split()
        minut, timme = delar[0], delar[1]

        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>se.riksdag-ai.gov-dokument-synk</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_sokvag}</string>
        <string>{script_abs}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>  <integer>{timme}</integer>
        <key>Minute</key><integer>{minut}</integer>
    </dict>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key>
    <string>{str(Path.home())}/Library/Logs/gov-dokument-synk.log</string>
    <key>StandardErrorPath</key>
    <string>{str(Path.home())}/Library/Logs/gov-dokument-synk-fel.log</string>
</dict>
</plist>"""
        with open(plist_fil, "w") as fh:
            fh.write(plist)

        subprocess.run(["launchctl", "load", str(plist_fil)], check=True)
        log.info(f"launchd-jobb installerat: {plist_fil}")
        log.info(f"Kör dagligen kl. {timme}:{minut}. Loggar: ~/Library/Logs/")

    else:
        # cron — fungerar på Linux och macOS
        rad = f"{cron_schema} {python_sokvag} {script_abs}\n"

        befintlig = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True
        ).stdout

        if script_abs in befintlig:
            log.info("Cron-jobb finns redan. Ingen ändring gjord.")
            return

        ny_crontab = befintlig + rad
        proc = subprocess.run(["crontab", "-"], input=ny_crontab, text=True)
        if proc.returncode == 0:
            log.info(f"Cron-jobb tillagt: {rad.strip()}")
        else:
            log.error("Kunde inte uppdatera crontab.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Daglig synkronisering av gov-data")
    parser.add_argument("--tvinga", action="store_true",
                        help="Hämta om listorna oavsett timestamp")
    parser.add_argument("--installera-schema", action="store_true",
                        help="Installera schemalagt jobb via cron eller launchd (se .env)")
    args = parser.parse_args()

    if args.installera_schema:
        python_sokvag = os.getenv("PYTHON_SOKVÄG", "python3")
        installera_schema(__file__, python_sokvag)
    else:
        kor(tvinga=args.tvinga)
