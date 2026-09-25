"""
pdf_lib.py — Biblioteksfunktioner för nedladdning och textextraktion av PDF:er.
Importeras av 02_initial_bulk.py, 03_synka_data.py och mcp_server.py.
"""
import os
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

import db
from pdftext_skydd import PdfResultat, extrahera_pdf

load_dotenv()

log = logging.getLogger(__name__)

_SCRIPT_DIR    = Path(__file__).parent


def _absolut_cache_sokvag(env_var: str, default_undermapp: str) -> Path:
    """Returnerar absolut sokvag till en cache-katalog.

    Relativa sökvägar (från env eller default) tolkas alltid relativt
    skriptets mapp — inte processens cwd. När Claude Desktop startar
    MCP-servern utan korrekt cwd blir annars `./pdf_cache` lika med `/pdf_cache`
    på ett read-only filsystem, och nedladdningen misslyckas.
    """
    raw = os.getenv(env_var, str(_SCRIPT_DIR / default_undermapp))
    p   = Path(raw).expanduser()
    if not p.is_absolute():
        p = (_SCRIPT_DIR / p).resolve()
    return p


PDF_CACHE_DIR   = _absolut_cache_sokvag("PDF_CACHE_DIR", "pdf_cache")
FORDROJNING     = float(os.getenv("PDF_DOWNLOAD_DELAY", "0.5"))
REGERINGEN_BAS  = "https://www.regeringen.se"

# Prefix för pdftext_skydd:s miljövariabler (GOV_OCR_SPRAK m.fl.) och
# OCR-språk när GOV_OCR_SPRAK saknas. Internationella överenskommelser har
# parallelltext på engelska, franska och tyska vid sidan av svenskan.
OCR_PREFIX        = "GOV"
OCR_STANDARDSPRAK = "swe+eng+fra+deu"


# Dokumenttyper som ska bulk-laddas ned
BULK_TYPER = {"1326", "2099", "1332"}
BULK_NAMN  = {"1326": "förordningsmotiv", "2099": "remissmissiv", "1332": "int. överenskommelser"}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "mcp-for-g0v_se/1.0 (+https://github.com/MagnusKolsjo/mcp-for-g0v_se)",
    "Referer": REGERINGEN_BAS,
})


def pdf_cache_sokvag(bilage_url: str) -> Path:
    """Beräknar en unik lokal sokvag för en PDF baserat på dess URL."""
    namn = bilage_url.strip("/").replace("/", "_")[-120:]
    if not namn.endswith(".pdf"):
        namn += ".pdf"
    return PDF_CACHE_DIR / namn


def ladda_ned_pdf(url: str, sokvag: Path) -> tuple[bool, str]:
    """Laddar ned en PDF till disk. Returnerar (True, "") vid lyckat resultat, annars (False, felmeddelande)."""
    full_url = fullstandig_url(url)
    try:
        svar = SESSION.get(full_url, timeout=30, stream=True)
        svar.raise_for_status()
        sokvag.parent.mkdir(parents=True, exist_ok=True)
        with open(sokvag, "wb") as f:
            for chunk in svar.iter_content(chunk_size=65536):
                f.write(chunk)
        return True, ""
    except Exception as e:
        log.warning(f"Nedladdning misslyckades ({url}): {e}")
        return False, str(e)


def extrahera(sokvag: Path | str, kalla_id: str = "", kalla_url: str = "") -> Optional[PdfResultat]:
    """Extraherar en PDF till markdown med pdftext_skydd.

    Extraktionen körs i en egen process under minnes- och tidsvakt, med
    OCR-språket GOV_OCR_SPRAK (standard swe+eng+fra+deu). Sidor utan
    textlager och block som fick läsas med ren textutvinning noteras i
    OCR-kön. Returnerar None om PDF:en inte gick att öppna.

    Flerspråkiga PDF:er (t.ex. internationella överenskommelser med
    parallelltext) returnerar blandad text; språkfiltreringen sker vid
    chunkningen.
    """
    kalla_id = kalla_id or Path(sokvag).name
    try:
        # Extraktionsprocessen skickar själv sina utskrifter till /dev/null,
        # så serverns stdout (JSON-RPC i stdio-läget) berörs inte.
        return extrahera_pdf(sokvag, prefix=OCR_PREFIX,
                             standardsprak=OCR_STANDARDSPRAK,
                             kalla_id=kalla_id, kalla_url=kalla_url)
    except Exception as e:
        log.warning(f"Textextraktion misslyckades ({Path(sokvag).name}): {e}")
        return None


def extrahera_text(sokvag: Path | str, kalla_id: str = "", kalla_url: str = "") -> Optional[str]:
    """Som extrahera(), men returnerar bara texten, eller None om den är
    tom eller kortare än 50 tecken."""
    res = extrahera(sokvag, kalla_id, kalla_url)
    text = res.text if res else None
    return text if text and len(text.strip()) > 50 else None


def fullstandig_url(url: str) -> str:
    """Bilage-URL:er i g0v-listorna är relativa regeringen.se."""
    return REGERINGEN_BAS + url if url.startswith("/") else url


def uppdatera_dokument_med_fulltext(doc_id: int, fulltext: str, sokvag: Path, conn):
    """Sparar extraherad fulltext och PDF-sokvag i databasen."""
    cur    = conn.cursor()
    tabell = f"{db._prefix()}dokument"
    ph     = db._ph()
    cur.execute(f"""
        UPDATE {tabell}
        SET fulltext_md = {ph},
            fulltext_hamtad_vid = {'NOW()' if db._ar_postgres() else 'CURRENT_TIMESTAMP'},
            pdf_sokvag = {ph}
        WHERE id = {ph}
    """, (fulltext, str(sokvag), doc_id))
    conn.commit()
    cur.close()




def behandla_ett_dokument(doc: dict, conn) -> str:
    """Laddar ned och extraherar text för ett enskilt dokument. Returnerar statussträng."""
    bilagor_raw = doc.get("bilagor")
    if isinstance(bilagor_raw, str):
        try:
            bilagor = json.loads(bilagor_raw)
        except Exception:
            bilagor = []
    else:
        bilagor = bilagor_raw or []

    if not bilagor:
        return f"HOPPAR (ingen bilaga): {doc.get('titel','')[:60]}"

    bilage_url = bilagor[0].get("url", "")
    if not bilage_url:
        return f"HOPPAR (tom bilage-URL): {doc.get('titel','')[:60]}"

    sokvag = pdf_cache_sokvag(bilage_url)

    if not sokvag.exists():
        ok, fel = ladda_ned_pdf(bilage_url, sokvag)
        if not ok:
            return f"FEL (nedladdning — {fel}): {doc.get('titel','')[:60]}"
        time.sleep(FORDROJNING)

    text = extrahera_text(sokvag, kalla_id=f"dokument:{doc['id']}",
                          kalla_url=fullstandig_url(bilage_url))
    if not text:
        return f"FEL (extraktion misslyckades): {doc.get('titel','')[:60]}"

    uppdatera_dokument_med_fulltext(doc["id"], text, sokvag, conn)

    # Radera PDF-filen direkt — fulltexten finns nu i databasen
    try:
        sokvag.unlink(missing_ok=True)
    except Exception as e:
        log.warning(f"Kunde inte radera PDF-fil {sokvag.name}: {e}")

    return f"OK ({len(text)} tecken): {doc.get('titel','')[:60]}"


def stada_pdf_cache(conn) -> dict:
    """Raderar PDF-filer vars fulltext finns i databasen och som är äldre
    än PDF_CACHE_TTL_DAGAR dagar (standard: 1 dag).

    Täcker tabellerna dokument, remissvar och arendeforteckning.
    Filer där fulltext_md IS NULL lämnas kvar för retry.
    Returnerar statistik: {raderade, bevarade, fel}.
    """
    ttl_dagar  = int(os.getenv("PDF_CACHE_TTL_DAGAR", "1"))
    gransvarde = datetime.now(timezone.utc) - timedelta(days=ttl_dagar)

    cur      = conn.cursor()
    sokvagar: list[str] = []
    tabeller = [
        f"{db._prefix()}dokument",
        f"{db._prefix()}remissvar",
        f"{db._prefix()}arendeforteckning",
    ]

    for tabell in tabeller:
        try:
            cur.execute(
                f"SELECT pdf_sokvag FROM {tabell} "
                f"WHERE fulltext_md IS NOT NULL AND pdf_sokvag IS NOT NULL"
            )
            sokvagar.extend(r[0] for r in cur.fetchall() if r[0])
        except Exception:
            pass  # Tabellen kanske inte finns i SQLite-installationer

    cur.close()

    raderade = bevarade = fel = 0
    for sokvag_str in sokvagar:
        fil = Path(sokvag_str)
        if not fil.exists():
            continue
        try:
            andrad = datetime.fromtimestamp(fil.stat().st_mtime, tz=timezone.utc)
            if andrad > gransvarde:
                bevarade += 1
                continue
        except Exception:
            pass
        try:
            fil.unlink()
            raderade += 1
        except Exception as e:
            log.warning(f"Kunde inte radera {fil.name}: {e}")
            fel += 1

    log.info(
        f"PDF-cache städad: {raderade} raderade, "
        f"{bevarade} bevarade (yngre än {ttl_dagar} dag(ar)), {fel} fel"
    )
    return {"raderade": raderade, "bevarade": bevarade, "fel": fel}


def hamta_dokument_for_bulk(typ_koder: set, hoppa_existerande: bool, conn) -> list[dict]:
    """Hämtar dokument ur databasen som ska bulk-laddas ned."""
    cur       = conn.cursor()
    tabell    = f"{db._prefix()}dokument"
    typ_lista = list(typ_koder)

    if db._ar_postgres():
        cur.execute(f"""
            SELECT id, url, typ_kod, titel, bilagor
            FROM {tabell}
            WHERE typ_kod = ANY(%s)
            {'AND fulltext_md IS NULL' if hoppa_existerande else ''}
            ORDER BY publicerad DESC NULLS LAST
        """, (typ_lista,))
    else:
        platser = ",".join(["?"] * len(typ_lista))
        cur.execute(f"""
            SELECT id, url, typ_kod, titel, bilagor
            FROM {tabell}
            WHERE typ_kod IN ({platser})
            {'AND fulltext_md IS NULL' if hoppa_existerande else ''}
            ORDER BY publicerad DESC
        """, typ_lista)

    rader = cur.fetchall()
    cur.close()
    return [
        {"id": r[0], "url": r[1], "typ_kod": r[2], "titel": r[3], "bilagor": r[4]}
        for r in rader
    ]


def kor(typer: Optional[set] = None, hoppa_existerande: bool = True):
    """
    Huvudfunktion för bulk-nedladdning.

    Args:
        typer:             Delmängd av BULK_TYPER att bearbeta (None = alla).
        hoppa_existerande: Om True hoppas dokument med befintlig fulltext över.
    """
    valda_typer = typer or BULK_TYPER

    log.info(f"Startar bulk-nedladdning för: {[BULK_NAMN[t] for t in valda_typer]}")

    conn     = db._hamta_db()
    dokument = hamta_dokument_for_bulk(valda_typer, hoppa_existerande, conn)
    log.info(f"{len(dokument)} dokument att bearbeta.")

    if not dokument:
        log.info("Ingenting att göra. Avslutar.")
        conn.close()
        return

    ok = fel = hoppade = 0
    for i, doc in enumerate(dokument, 1):
        status = behandla_ett_dokument(doc, conn)
        if status.startswith("OK"):
            ok += 1
        elif status.startswith("HOPPAR"):
            hoppade += 1
        else:
            fel += 1
        if i % 50 == 0 or i == len(dokument):
            log.info(f"  [{i}/{len(dokument)}] {status}")
        else:
            log.debug(f"  [{i}/{len(dokument)}] {status}")

    conn.close()
    log.info(f"Klart. OK: {ok}, Hoppade: {hoppade}, Fel: {fel}")
