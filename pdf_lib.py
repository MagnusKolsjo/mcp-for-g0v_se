"""
pdf_lib.py — Biblioteksfunktioner för nedladdning och textextraktion av PDF:er.
Importeras av 02_initial_bulk.py, 03_synka_data.py och mcp_server.py.
"""
import os
import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
import pymupdf4llm
from dotenv import load_dotenv

import db

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


import contextlib


@contextlib.contextmanager
def _tysta_subprocess_stdout():
    """Redirigerar OS-nivåns stdout (FD 1) och stderr (FD 2) till loggfil
    under bullriga anrop.

    pymupdf4llm anropar internt Tesseract/OCR-bibliotek via C-bindningar
    som skriver direkt till FD 1 — utan att gå via Python:s sys.stdout.
    I MCP-stdio-protokollet är FD 1 reserverad för JSON-RPC, så varje
    okontrollerad utskrift från subprocesses krossar protokollet och
    triggar popup-varningar i Claude Desktop.

    Lösningen är en OS-nivå dup2-redirigering: FD 1 och FD 2 pekas om
    till loggfilen under det bullriga anropet, och återställs efteråt.
    Python:s sys.stdout/sys.stderr berörs inte (för MCP-protokollet
    behåller dem så att JSON-RPC-svar fortsätter fungera utanför
    contextmanagern).
    """
    log_path = _SCRIPT_DIR / "logs" / "subprocess.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    save_out = os.dup(1)
    save_err = os.dup(2)
    log_fd   = os.open(str(log_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        yield
    finally:
        os.dup2(save_out, 1)
        os.dup2(save_err, 2)
        os.close(save_out)
        os.close(save_err)
        os.close(log_fd)


# Dokumenttyper som ska bulk-laddas ned
BULK_TYPER = {"1326", "2099", "1332"}
BULK_NAMN  = {"1326": "förordningsmotiv", "2099": "remissmissiv", "1332": "int. överenskommelser"}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; gov-dokument-mcp/1.0; riksdag-ai-research)",
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
    full_url = REGERINGEN_BAS + url if url.startswith("/") else url
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


def extrahera_text(sokvag: Path) -> Optional[str]:
    """
    Extraherar text från en PDF med pymupdf4llm.
    Returnerar Markdown-text eller None vid fel.

    OBS: Flerspråkiga PDF:er (t.ex. internationella överenskommelser med
    parallelltext) returnerar blandad text. Språkfiltrering läggs till
    i kommande version.

    pymupdf4llm:s C-backends skriver diagnostikmeddelanden direkt till
    FD 1 — vi tystar dem under anropet för att inte korrumpera
    MCP-stdio-protokollet.
    """
    try:
        with _tysta_subprocess_stdout():
            text = pymupdf4llm.to_markdown(str(sokvag))
        return text if text and len(text.strip()) > 50 else None
    except Exception as e:
        log.warning(f"Textextraktion misslyckades ({sokvag.name}): {e}")
        return None


def uppdatera_dokument_med_fulltext(doc_id: int, fulltext: str, sokvag: Path,
                                    conn, use_postgres: bool):
    """Sparar extraherad fulltext och PDF-sokvag i databasen."""
    cur = conn.cursor()
    tabell = "gov_data.dokument" if use_postgres else "dokument"
    plats  = "%s" if use_postgres else "?"
    cur.execute(f"""
        UPDATE {tabell}
        SET fulltext_md = {plats},
            fulltext_hamtad_vid = {'NOW()' if use_postgres else 'CURRENT_TIMESTAMP'},
            pdf_sokvag = {plats}
        WHERE id = {plats}
    """, (fulltext, str(sokvag), doc_id))
    conn.commit()
    cur.close()



def ocr_pdf(sokvag: Path) -> Optional[Path]:
    """
    Kör OCR på en bildbaserad PDF med ocrmypdf och Tesseract.
    Skapar en ny PDF med inbäddat texlager bredvid originalet (_ocr-suffix).
    Returnerar sökvägen till den OCR-behandlade filen, eller None vid fel.

    Kräver att Tesseract och svenska språkpaket är installerade:
      Linux:  apt install tesseract-ocr tesseract-ocr-swe
      macOS:  brew install tesseract tesseract-lang
    """
    try:
        import ocrmypdf
    except ImportError:
        log.warning("ocrmypdf är inte installerat — hoppar över OCR-fallback")
        return None

    ocr_sokvag = sokvag.with_name(sokvag.stem + "_ocr" + sokvag.suffix)
    try:
        # Tesseract som C-binär skriver progress till FD 1 även med
        # quiet=True — använd FD-redirigering för MCP-säkerhet.
        with _tysta_subprocess_stdout():
            ocrmypdf.ocr(
                str(sokvag),
                str(ocr_sokvag),
                language="swe+eng+fra+deu",
                progress_bar=False,
                quiet=True,
            )
        log.info(f"OCR klar: {ocr_sokvag.name}")
        return ocr_sokvag
    except Exception as e:
        log.warning(f"OCR misslyckades ({sokvag.name}): {e}")
        return None

def behandla_ett_dokument(doc: dict, conn, use_postgres: bool) -> str:
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

    ocr_sokvag = None
    text = extrahera_text(sokvag)
    if not text:
        log.info(f"Ingen text — försöker OCR-fallback: {sokvag.name}")
        ocr_sokvag = ocr_pdf(sokvag)
        if ocr_sokvag:
            text = extrahera_text(ocr_sokvag)
    if not text:
        return f"FEL (extraktion misslyckades även efter OCR): {doc.get('titel','')[:60]}"

    uppdatera_dokument_med_fulltext(doc["id"], text, sokvag, conn, use_postgres)

    # Radera PDF-filen direkt — fulltexten finns nu i databasen
    for fil in [sokvag, ocr_sokvag]:
        if fil and fil.exists():
            try:
                fil.unlink()
            except Exception as e:
                log.warning(f"Kunde inte radera PDF-fil {fil.name}: {e}")

    return f"OK ({len(text)} tecken): {doc.get('titel','')[:60]}"


def stada_pdf_cache(conn, use_postgres: bool) -> dict:
    """Raderar PDF-filer vars fulltext finns i databasen och som är äldre
    än PDF_CACHE_TTL_DAGAR dagar (standard: 1 dag).

    Täcker tabellerna dokument, remissvar och arendeforteckning.
    Filer där fulltext_md IS NULL lämnas kvar för retry.
    Returnerar statistik: {raderade, bevarade, fel}.
    """
    ttl_dagar  = int(os.getenv("PDF_CACHE_TTL_DAGAR", "1"))
    gransvarde = datetime.utcnow() - timedelta(days=ttl_dagar)

    cur = conn.cursor()
    sokvagar: list[str] = []

    tabeller_pg  = ["gov_data.dokument", "gov_data.remissvar", "gov_data.arendeforteckning"]
    tabeller_sql = ["dokument", "remissvar", "arendeforteckning"]
    tabeller     = tabeller_pg if use_postgres else tabeller_sql

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
            andrad = datetime.utcfromtimestamp(fil.stat().st_mtime)
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


def hamta_dokument_for_bulk(typ_koder: set, hoppa_existerande: bool,
                             conn, use_postgres: bool) -> list[dict]:
    """Hämtar dokument ur databasen som ska bulk-laddas ned."""
    cur    = conn.cursor()
    tabell = "gov_data.dokument" if use_postgres else "dokument"
    typ_lista = list(typ_koder)

    if use_postgres:
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
    valda_typer  = typer or BULK_TYPER
    use_postgres = db.DATABASE_URL.startswith("postgresql")

    log.info(f"Startar bulk-nedladdning för: {[BULK_NAMN[t] for t in valda_typer]}")

    conn      = db.get_conn()
    dokument  = hamta_dokument_for_bulk(valda_typer, hoppa_existerande, conn, use_postgres)
    log.info(f"{len(dokument)} dokument att bearbeta.")

    if not dokument:
        log.info("Ingenting att göra. Avslutar.")
        conn.close()
        return

    ok = fel = hoppade = 0
    for i, doc in enumerate(dokument, 1):
        status = behandla_ett_dokument(doc, conn, use_postgres)
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
