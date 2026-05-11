"""
hamta_listor_lib.py — Biblioteksfunktioner för hämtning av JSON-listor från g0v.se.
Importeras av 03_synka_data.py. Körbar ingångspunkt: 01_hamta_listor.py.
"""
import os
import json
import time
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

import db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

G0V_BAS       = "https://g0v.se"

_SCRIPT_DIR = Path(__file__).parent


def _absolut_cache_sokvag(env_var: str, default_undermapp: str) -> Path:
    """Returnerar absolut sokvag till en cache-katalog.

    Relativa sökvägar (från env eller default) tolkas alltid relativt
    skriptets mapp — inte processens cwd. När Claude Desktop startar
    MCP-servern utan korrekt cwd blir annars `./json_cache` lika med
    `/json_cache` på ett read-only filsystem.
    """
    raw = os.getenv(env_var, str(_SCRIPT_DIR / default_undermapp))
    p   = Path(raw).expanduser()
    if not p.is_absolute():
        p = (_SCRIPT_DIR / p).resolve()
    return p


JSON_CACHE_DIR = _absolut_cache_sokvag("JSON_CACHE_DIR", "json_cache")

# De fem JSON-listorna vi hämtar, med primär typkod
LISTOR = [
    ("rattsliga-dokument/lagradsremiss",                             "2085"),
    ("remisser",                                                      "2099"),
    ("kommenterade-dagordningar",                                    "2098"),
    ("rattsliga-dokument/forordningsmotiv",                          "1326"),
    ("rattsliga-dokument/sveriges-internationella-overenskommelser", "1332"),
]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "gov-dokument-mcp/1.0 (riksdag-ai-research)"})


def hamta_senast_uppdaterad() -> str:
    """Hämtar senaste uppdateringstidsstämpel från g0v.se."""
    svar = SESSION.get(f"{G0V_BAS}/api/latest_updated.json", timeout=10)
    svar.raise_for_status()
    return svar.json()["latest_updated"]


def hamta_lista(endpoint: str) -> list[dict]:
    """Hämtar en JSON-lista från g0v.se och cachar lokalt."""
    filnamn = endpoint.replace("/", "_") + ".json"
    cache_sokvag = JSON_CACHE_DIR / filnamn

    log.info(f"Hämtar {G0V_BAS}/{endpoint}.json ...")
    svar = SESSION.get(f"{G0V_BAS}/{endpoint}.json", timeout=30)
    svar.raise_for_status()
    data = svar.json()

    cache_sokvag.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_sokvag, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    log.info(f"  {len(data)} poster hämtade och cachade.")
    return data


def _json_eller_none(varde) -> Optional[str]:
    """Serialiserar ett varde till JSON-sträng, eller None om tomt."""
    if varde is None:
        return None
    return json.dumps(varde, ensure_ascii=False)


def upsert_dokument(poster: list[dict], typ_kod: str, conn, use_postgres: bool):
    """Lägger till eller uppdaterar dokumentposter i databasen.

    OBS: Fältnamnen i g0v.se:s JSON-svar är på engelska
    (published, updated, types, senders, categories,
    shortcuts, attachments). De ASCII-svenska kolumnnamnen i databasen
    (publicerad, typer, avsandare, kategorier, genvagar,
    bilagor osv.) gäller bara för vår egen tabellstruktur. Att läsa
    g0v.se-fälten med svenska namn ger None och tappar tyst datakvalitet.
    """
    cur = conn.cursor()
    tabell = "gov_data.dokument" if use_postgres else "dokument"

    for post in poster:
        url = post.get("url", "")
        if not url:
            continue

        def parse_datum(s):
            if not s:
                return None
            try:
                return date.fromisoformat(s)
            except ValueError:
                return None

        publicerad = parse_datum(post.get("published"))
        uppdaterad = parse_datum(post.get("updated"))

        if use_postgres:
            cur.execute(f"""
                INSERT INTO {tabell}
                    (url, typ_kod, titel, sammanfattning, publicerad, uppdaterad,
                     typer, avsandare, kategorier, genvagar, bilagor)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (url) DO UPDATE SET
                    titel          = EXCLUDED.titel,
                    sammanfattning = EXCLUDED.sammanfattning,
                    publicerad      = EXCLUDED.publicerad,
                    uppdaterad        = EXCLUDED.uppdaterad,
                    typer          = EXCLUDED.typer,
                    avsandare        = EXCLUDED.avsandare,
                    kategorier     = EXCLUDED.kategorier,
                    genvagar      = EXCLUDED.genvagar,
                    bilagor    = EXCLUDED.bilagor
            """, (
                url, typ_kod,
                post.get("title"), post.get("summary"),
                publicerad, uppdaterad,
                post.get("types"), post.get("senders"), post.get("categories"),
                json.dumps(post.get("shortcuts") or [], ensure_ascii=False),
                json.dumps(post.get("attachments") or [], ensure_ascii=False),
            ))
        else:
            cur.execute(f"""
                INSERT OR REPLACE INTO {tabell}
                    (url, typ_kod, titel, sammanfattning, publicerad, uppdaterad,
                     typer, avsandare, kategorier, genvagar, bilagor)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                url, typ_kod,
                post.get("title"), post.get("summary"),
                str(publicerad) if publicerad else None,
                str(uppdaterad) if uppdaterad else None,
                _json_eller_none(post.get("types")),
                _json_eller_none(post.get("senders")),
                _json_eller_none(post.get("categories")),
                _json_eller_none(post.get("shortcuts")),
                _json_eller_none(post.get("attachments")),
            ))

    conn.commit()
    cur.close()


def kor(tvinga: bool = False):
    """
    Huvudfunktion. Kontrollerar om ny data finns och uppdaterar databasen.

    Args:
        tvinga: Om True hämtas listorna oavsett om ny data finns.
    """
    db.initiera_schema()

    senaste_kand = db.hamta_synkstatus("g0v_latest_updated")
    aktuell      = hamta_senast_uppdaterad()
    log.info(f"g0v.se senast uppdaterad: {aktuell} (senast känd: {senaste_kand})")

    if not tvinga and senaste_kand == aktuell:
        log.info("Ingen ny data på g0v.se. Avslutar.")
        return

    conn          = db.get_conn()
    use_postgres  = db.DATABASE_URL.startswith("postgresql")
    totalt        = 0

    for endpoint, typ_kod in LISTOR:
        poster = hamta_lista(endpoint)
        upsert_dokument(poster, typ_kod, conn, use_postgres)
        totalt += len(poster)
        time.sleep(0.5)

    conn.close()

    db.spara_synkstatus("g0v_latest_updated", aktuell)
    db.spara_synkstatus("listor_senast_hämtade", datetime.utcnow().isoformat())
    log.info(f"Klart. {totalt} poster behandlade.")
