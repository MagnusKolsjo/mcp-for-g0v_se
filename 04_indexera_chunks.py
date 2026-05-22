"""
04_indexera_chunks.py — Fristående bulk-chunkning och embedding av regeringsdokument.

Behandlar alla dokument med fulltext_md men utan rader i document_chunks.
Körs direkt i terminalen utan MCP-timeout.

Körning:
    ~/MCP-Servers/.venv/bin/python3 04_indexera_chunks.py

Loggar hopppade dokument till fel_indexering.log i samma mapp.
"""

import logging
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Ladda .env från skriptets mapp
_SCRIPT_DIR = Path(__file__).parent
load_dotenv(_SCRIPT_DIR / ".env")

# Lägg till skriptmappen i sökvägen så att db.py hittas
sys.path.insert(0, str(_SCRIPT_DIR))
import db

# ---------------------------------------------------------------------------
# Loggning — konsol + fil
# ---------------------------------------------------------------------------

_loggfil = _SCRIPT_DIR / "fel_indexering.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_loggfil, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Embeddingmodell och språkdetektor (lazy-laddade, kopierade från mcp_server.py)
# ---------------------------------------------------------------------------

_modell   = None
_detektor = None


def _hamta_modell():
    global _modell
    if _modell is None:
        from sentence_transformers import SentenceTransformer
        modell_namn = os.getenv("EMBEDDING_MODEL", "KBLab/sentence-bert-swedish-cased")
        log.info(f"Laddar embeddingmodell: {modell_namn}")
        _modell = SentenceTransformer(modell_namn)
    return _modell


def _hamta_detektor():
    global _detektor
    if _detektor is not None:
        return _detektor
    try:
        from lingua import Language, LanguageDetectorBuilder
        _detektor = LanguageDetectorBuilder.from_languages(
            Language.SWEDISH,
            Language.ENGLISH,
            Language.FRENCH,
            Language.GERMAN,
            Language.BOKMAL,
            Language.NYNORSK,
            Language.DANISH,
        ).build()
    except ImportError:
        log.warning("lingua-language-detector ej installerat — språkfiltrering inaktiv")
        _detektor = False
    return _detektor


def _ar_svensk(text: str) -> bool:
    if len(text.strip()) < 80:
        return True
    detektor = _hamta_detektor()
    if not detektor:
        return True
    try:
        from lingua import Language
        return detektor.detect_language_of(text) == Language.SWEDISH
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Chunkning (kopierad från mcp_server.py)
# ---------------------------------------------------------------------------

def _chunka_text(
    text: str,
    max_tecken: int = 800,
    overlap_tecken: int = 200,
) -> list[str]:
    stycken  = [s.strip() for s in text.split("\n\n") if s.strip()]
    chunks: list[str] = []
    aktuell: list[str] = []
    aktuell_len = 0

    for stycke in stycken:
        stycke_len    = len(stycke)
        separator_len = 2 if aktuell else 0
        if aktuell and aktuell_len + separator_len + stycke_len > max_tecken:
            chunk_text   = "\n\n".join(aktuell)
            chunks.append(chunk_text)
            overlap_text = chunk_text[-overlap_tecken:] if len(chunk_text) > overlap_tecken else chunk_text
            aktuell      = [overlap_text, stycke]
            aktuell_len  = len(overlap_text) + 2 + stycke_len
        else:
            aktuell.append(stycke)
            aktuell_len += separator_len + stycke_len

    if aktuell:
        chunks.append("\n\n".join(aktuell))

    return [c for c in chunks if len(c.strip()) > 50]


# ---------------------------------------------------------------------------
# Indexering av ett enskilt dokument
# ---------------------------------------------------------------------------

def _indexera_dokument(dokument_id: int, fulltext: str, conn) -> int:
    """
    Chunkar och embeddar ett dokument. Returnerar antal indexerade chunks.
    Kastar undantag vid fel — fångas av anroparen.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM gov_data.document_chunks WHERE dokument_id = %s",
        (dokument_id,)
    )
    if cur.fetchone()[0] > 0:
        cur.close()
        return 0  # Redan indexerat

    chunks = _chunka_text(fulltext)
    if not chunks:
        cur.close()
        return 0

    svenska_chunks = [c for c in chunks if _ar_svensk(c)]
    if not svenska_chunks:
        svenska_chunks = chunks

    modell = _hamta_modell()
    cur.execute(
        "DELETE FROM gov_data.document_chunks WHERE dokument_id = %s",
        (dokument_id,)
    )
    for i, chunk in enumerate(svenska_chunks):
        vektor = modell.encode(chunk).tolist()
        cur.execute("""
            INSERT INTO gov_data.document_chunks
                (dokument_id, chunk_index, chunk_text, embedding)
            VALUES (%s, %s, %s, %s)
        """, (dokument_id, i, chunk, vektor))

    conn.commit()
    cur.close()
    return len(svenska_chunks)


# ---------------------------------------------------------------------------
# Huvudloop
# ---------------------------------------------------------------------------

def kor():
    if not db._ar_postgres():
        log.error("Bulk-indexering kräver PostgreSQL med pgvector. Kontrollera DATABASE_URL i .env.")
        sys.exit(1)

    conn = db._hamta_db()
    cur  = conn.cursor()

    log.info("Hämtar dokument med fulltext_md men utan chunks...")
    cur.execute("""
        SELECT d.id, d.url, length(d.fulltext_md) AS fulltext_langd
        FROM gov_data.dokument d
        WHERE d.fulltext_md IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM gov_data.document_chunks dc
              WHERE dc.dokument_id = d.id
          )
        ORDER BY d.id
    """)
    dokument = cur.fetchall()
    cur.close()

    totalt        = len(dokument)
    indexerade    = 0
    hoppade       = 0
    chunks_totalt = 0

    log.info(f"Hittade {totalt} dokument att indexera.")
    log.info(f"Hoppade dokument loggas till {_loggfil}")
    log.info("-" * 60)

    for nummer, (dok_id, url, fulltext_langd) in enumerate(dokument, start=1):
        # Hämta fulltexten
        cur2 = conn.cursor()
        cur2.execute(
            "SELECT fulltext_md FROM gov_data.dokument WHERE id = %s",
            (dok_id,)
        )
        rad = cur2.fetchone()
        cur2.close()

        if not rad or not rad[0]:
            continue

        fulltext    = rad[0]
        antal_chunks = len(_chunka_text(fulltext))

        try:
            antal = _indexera_dokument(dok_id, fulltext, conn)
            chunks_totalt += antal
            indexerade    += 1
            log.info(
                f"[{nummer}/{totalt}] OK — id={dok_id} "
                f"fulltext={fulltext_langd} tecken "
                f"chunks={antal} "
                f"url={url}"
            )
        except Exception as exc:
            hoppade += 1
            feltyp  = type(exc).__name__
            felmedd = str(exc)
            log.error(
                f"[{nummer}/{totalt}] FEL — id={dok_id} "
                f"fulltext={fulltext_langd} tecken "
                f"beraknade_chunks={antal_chunks} "
                f"feltyp={feltyp} "
                f"url={url} "
                f"fel={felmedd}"
            )
            log.debug(traceback.format_exc())

    conn.close()

    log.info("=" * 60)
    log.info(f"Klart. Indexerade: {indexerade}, hoppade: {hoppade}, chunks totalt: {chunks_totalt}")
    if hoppade:
        log.info(f"Se {_loggfil} för detaljer om de {hoppade} hoppade dokumenten.")


if __name__ == "__main__":
    kor()
