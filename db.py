"""
Databaslager for gov_data-schemat.
Stoder PostgreSQL (med pgvector) och SQLite — valet gors via DATABASE_URL i miljon.

Exporterar:
    _ar_postgres()  — True om aktiv backend ar PostgreSQL
    _hamta_db()     — returnerar en ny anslutning per anrop
    _ph()           — frageplacerare ('%s' eller '?')
    _prefix()       — schemaprefix ('gov_data.' eller '')
    initiera_schema()
    hamta_synkstatus(nyckel)
    spara_synkstatus(nyckel, varde)
    rensa_utgangna_remissvar()
"""
import os
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

_log = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///gov_cache.db")

_SCRIPT_DIR = Path(__file__).parent


# ============================================================
# Anslutningshjalpare — ett anrop, en anslutning
# ============================================================

def _ar_postgres() -> bool:
    """True om aktiv backend ar PostgreSQL."""
    return DATABASE_URL.startswith("postgresql")


def _hamta_db():
    """Returnerar en ny databasanslutning per anrop."""
    if _ar_postgres():
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    db_path = DATABASE_URL.replace("sqlite:///", "")
    return sqlite3.connect(db_path)


def _ph() -> str:
    """Frageplacerare for parametriserade fragor ('%s' eller '?')."""
    return "%s" if _ar_postgres() else "?"


def _prefix() -> str:
    """Schemaprefix for tabellnamn ('gov_data.' eller '')."""
    return "gov_data." if _ar_postgres() else ""


# ============================================================
# Schemainitalisering
# ============================================================

def initiera_schema():
    """Skapar gov_data-schemat och tabellerna om de inte finns.

    Laser DDL fran db/schema_postgres.sql eller db/schema_sqlite.sql
    beroende pa aktiv backend. Robust mot att databasen ar nere vid
    uppstart — felet loggas och servern fortsatter.
    """
    sql_fil = (
        _SCRIPT_DIR / "db" / "schema_postgres.sql"
        if _ar_postgres()
        else _SCRIPT_DIR / "db" / "schema_sqlite.sql"
    )

    try:
        sql = sql_fil.read_text(encoding="utf-8")
    except FileNotFoundError:
        _log.error("Schemafil saknas: %s", sql_fil)
        return

    try:
        conn = _hamta_db()
        cur  = conn.cursor()

        if _ar_postgres():
            # psycopg2 hanterar inte hela skript som ett block —
            # dela upp pa satsgrans och kor sats for sats.
            satser = [s.strip() for s in sql.split(";") if s.strip()]
            for sats in satser:
                cur.execute(sats)
        else:
            # sqlite3 accepterar executescript for flera satser i ett block.
            cur.executescript(sql)

        conn.commit()
        cur.close()
        conn.close()
        _log.info("Schema initierat.")

    except Exception as exc:  # noqa: BLE001
        # Servern ska starta även om databasen ar otillganglig.
        _log.warning("Schema-init misslyckades (databasen otillganglig?): %s", exc)


# ============================================================
# Synkstatus
# ============================================================

def hamta_synkstatus(nyckel: str) -> Optional[str]:
    """Hämtar ett synkstatus-varde ur databasen."""
    conn = _hamta_db()
    cur  = conn.cursor()
    ph   = _ph()
    tab  = f"{_prefix()}synkstatus"
    cur.execute(f"SELECT varde FROM {tab} WHERE nyckel = {ph}", (nyckel,))
    rad = cur.fetchone()
    cur.close()
    conn.close()
    return rad[0] if rad else None


def spara_synkstatus(nyckel: str, varde: str):
    """Sparar ett synkstatus-varde i databasen (upsert)."""
    conn = _hamta_db()
    cur  = conn.cursor()
    tab  = f"{_prefix()}synkstatus"

    if _ar_postgres():
        cur.execute(f"""
            INSERT INTO {tab} (nyckel, varde, uppdaterad)
            VALUES (%s, %s, NOW())
            ON CONFLICT (nyckel) DO UPDATE
                SET varde = EXCLUDED.varde, uppdaterad = NOW()
        """, (nyckel, varde))
    else:
        cur.execute(f"""
            INSERT OR REPLACE INTO {tab} (nyckel, varde, uppdaterad)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (nyckel, varde))

    conn.commit()
    cur.close()
    conn.close()


# ============================================================
# Underhall
# ============================================================

def rensa_utgangna_remissvar():
    """Tar bort remissvar vars TTL har passerat."""
    conn = _hamta_db()
    cur  = conn.cursor()
    tab  = f"{_prefix()}remissvar"

    if _ar_postgres():
        cur.execute(f"""
            DELETE FROM {tab}
            WHERE cache_utgar_vid IS NOT NULL
              AND cache_utgar_vid < NOW()
        """)
    else:
        cur.execute(f"""
            DELETE FROM {tab}
            WHERE cache_utgar_vid IS NOT NULL
              AND cache_utgar_vid < CURRENT_TIMESTAMP
        """)

    borttagna = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    if borttagna:
        _log.info("Rensade %d utgangna remissvar.", borttagna)
    return borttagna


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    initiera_schema()
