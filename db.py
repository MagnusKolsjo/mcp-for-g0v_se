"""
Databaslager för gov_data-schemat.
Stöder PostgreSQL (primär, med pgvector) och SQLite (fallback).
"""
import os
import logging
import json
import sqlite3
from datetime import datetime, date
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

_log = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///gov_cache.db")
_USE_POSTGRES = DATABASE_URL.startswith("postgresql")


def _get_pg_conn():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def _get_sqlite_conn():
    db_path = DATABASE_URL.replace("sqlite:///", "")
    return sqlite3.connect(db_path)


def get_conn():
    """Returnerar en databasanslutning."""
    if _USE_POSTGRES:
        return _get_pg_conn()
    return _get_sqlite_conn()


def initiera_schema():
    """Skapar gov_data-schemat och tabellerna om de inte finns."""
    conn = get_conn()
    cur = conn.cursor()

    if _USE_POSTGRES:
        cur.execute("CREATE SCHEMA IF NOT EXISTS gov_data")
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS gov_data.dokument (
                id                   SERIAL PRIMARY KEY,
                url                  TEXT UNIQUE NOT NULL,
                typ_kod              TEXT NOT NULL,
                titel                TEXT,
                sammanfattning       TEXT,
                publicerad            DATE,
                uppdaterad              DATE,
                typer                TEXT[],
                avsandare              TEXT[],
                kategorier           TEXT[],
                genvagar            JSONB,
                bilagor          JSONB,
                fulltext_md          TEXT,
                fulltext_hamtad_vid  TIMESTAMPTZ,
                pdf_sokvag           TEXT,
                indexerad_vid        TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS gov_data.document_chunks (
                id           SERIAL PRIMARY KEY,
                dokument_id  INT REFERENCES gov_data.dokument(id) ON DELETE CASCADE,
                chunk_index  INT,
                chunk_text   TEXT,
                embedding    vector(768)
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_gov_chunks_embedding
            ON gov_data.document_chunks
            USING ivfflat (embedding vector_cosine_ops)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS gov_data.beslut (
                id                        SERIAL PRIMARY KEY,
                titel                     TEXT,
                regeringsarendenummer     TEXT,
                diarienummer_text         TEXT,
                ansvarig_chefstjansteman  TEXT,
                vecka_url                 TEXT,
                vecka_nummer              INT,
                vecka_ar                  INT,
                statsrad                  TEXT,
                departement               TEXT,
                indexerad_vid             TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS gov_data.beslut_diarienummer (
                id            SERIAL PRIMARY KEY,
                beslut_id     INT REFERENCES gov_data.beslut(id) ON DELETE CASCADE,
                diarienummer  TEXT NOT NULL,
                komplett      BOOLEAN
            )
        """)


        cur.execute("""
            CREATE TABLE IF NOT EXISTS gov_data.remissvar (
                id                  SERIAL PRIMARY KEY,
                remiss_id           INT REFERENCES gov_data.dokument(id) ON DELETE CASCADE,
                remissinstans       TEXT NOT NULL,
                bilage_url      TEXT UNIQUE NOT NULL,
                fulltext_md         TEXT,
                fulltext_hamtad_vid TIMESTAMPTZ,
                cache_utgar_vid    TIMESTAMPTZ,
                pdf_sokvag          TEXT,
                indexerad_vid       TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS gov_data.remissvar_chunks (
                id            SERIAL PRIMARY KEY,
                remissvar_id  INT REFERENCES gov_data.remissvar(id) ON DELETE CASCADE,
                chunk_index   INT,
                chunk_text    TEXT,
                remissinstans TEXT NOT NULL,
                remiss_url    TEXT NOT NULL,
                embedding     vector(768)
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_remissvar_chunks_embedding
            ON gov_data.remissvar_chunks
            USING ivfflat (embedding vector_cosine_ops)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_remissvar_chunks_remiss
            ON gov_data.remissvar_chunks(remiss_url)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_beslut_dnr
            ON gov_data.beslut_diarienummer(diarienummer)
        """)

        # Migration: lägg till vecka_nummer och vecka_ar om de saknas (idempotent)
        cur.execute("""
            ALTER TABLE gov_data.beslut
            ADD COLUMN IF NOT EXISTS vecka_nummer INT
        """)
        cur.execute("""
            ALTER TABLE gov_data.beslut
            ADD COLUMN IF NOT EXISTS vecka_ar INT
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_beslut_vecka
            ON gov_data.beslut(vecka_ar, vecka_nummer)
        """)
        # Datamigration: populera vecka_nummer/vecka_ar för befintliga rader
        # som har NULL i dessa kolumner men har vecka_url satt (idempotent).
        cur.execute("""
            UPDATE gov_data.beslut
            SET
                vecka_nummer = (
                    regexp_match(vecka_url, 'vecka-(\\d+)-(\\d{4})')
                )[1]::int,
                vecka_ar = (
                    regexp_match(vecka_url, 'vecka-(\\d+)-(\\d{4})')
                )[2]::int
            WHERE vecka_ar IS NULL
              AND vecka_url IS NOT NULL
              AND vecka_url ~ 'vecka-\\d+-\\d{4}'
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS gov_data.arendeforteckning (
                id              SERIAL PRIMARY KEY,
                vecka_sida_url  TEXT NOT NULL,
                vecka_nummer    INT,
                vecka_ar        INT,
                datum           DATE,
                departement     TEXT,
                pdf_url         TEXT UNIQUE NOT NULL,
                pdf_sokvag      TEXT,
                fulltext_md     TEXT,
                indexerad_vid   TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_af_vecka
            ON gov_data.arendeforteckning(vecka_ar, vecka_nummer)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_af_departement
            ON gov_data.arendeforteckning(departement)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gov_data.arendeforteckning_chunks (
                id                      SERIAL PRIMARY KEY,
                arendeforteckning_id    INT REFERENCES gov_data.arendeforteckning(id) ON DELETE CASCADE,
                chunk_index             INT,
                chunk_text              TEXT,
                embedding               vector(768)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_af_chunks_embedding
            ON gov_data.arendeforteckning_chunks
            USING ivfflat (embedding vector_cosine_ops)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS gov_data.synkstatus (
                nyckel   TEXT PRIMARY KEY,
                varde    TEXT,
                uppdaterad TIMESTAMPTZ DEFAULT NOW()
            )
        """)

    else:
        # SQLite — utan vektorsökning, utan schemanamn
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS dokument (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                url                  TEXT UNIQUE NOT NULL,
                typ_kod              TEXT NOT NULL,
                titel                TEXT,
                sammanfattning       TEXT,
                publicerad            TEXT,
                uppdaterad              TEXT,
                typer                TEXT,
                avsandare              TEXT,
                kategorier           TEXT,
                genvagar            TEXT,
                bilagor          TEXT,
                fulltext_md          TEXT,
                fulltext_hamtad_vid  TEXT,
                pdf_sokvag           TEXT,
                indexerad_vid        TEXT DEFAULT CURRENT_TIMESTAMP
            );


            CREATE TABLE IF NOT EXISTS remissvar (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                remiss_id           INTEGER REFERENCES dokument(id) ON DELETE CASCADE,
                remissinstans       TEXT NOT NULL,
                bilage_url      TEXT UNIQUE NOT NULL,
                fulltext_md         TEXT,
                fulltext_hamtad_vid TEXT,
                cache_utgar_vid    TEXT,
                pdf_sokvag          TEXT,
                indexerad_vid       TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS beslut (
                id                        INTEGER PRIMARY KEY AUTOINCREMENT,
                titel                     TEXT,
                regeringsarendenummer     TEXT,
                diarienummer_text          TEXT,
                ansvarig_chefstjansteman  TEXT,
                vecka_url                 TEXT,
                statsrad                  TEXT,
                departement               TEXT,
                indexerad_vid             TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS beslut_diarienummer (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                beslut_id     INTEGER REFERENCES beslut(id) ON DELETE CASCADE,
                diarienummer  TEXT NOT NULL,
                komplett      INTEGER
            );

            CREATE TABLE IF NOT EXISTS synkstatus (
                nyckel     TEXT PRIMARY KEY,
                varde      TEXT,
                uppdaterad TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)

    conn.commit()
    cur.close()
    conn.close()
    _log.info("Schema initierat.")


def hamta_synkstatus(nyckel: str) -> Optional[str]:
    """Hämtar ett synkstatus-varde ur databasen."""
    conn = get_conn()
    cur = conn.cursor()
    tabell = "gov_data.synkstatus" if _USE_POSTGRES else "synkstatus"
    cur.execute(f"SELECT varde FROM {tabell} WHERE nyckel = %s" if _USE_POSTGRES
                else f"SELECT varde FROM {tabell} WHERE nyckel = ?", (nyckel,))
    rad = cur.fetchone()
    cur.close()
    conn.close()
    return rad[0] if rad else None


def spara_synkstatus(nyckel: str, varde: str):
    """Sparar ett synkstatus-varde i databasen (upsert)."""
    conn = get_conn()
    cur = conn.cursor()
    tabell = "gov_data.synkstatus" if _USE_POSTGRES else "synkstatus"
    if _USE_POSTGRES:
        cur.execute(f"""
            INSERT INTO {tabell} (nyckel, varde, uppdaterad)
            VALUES (%s, %s, NOW())
            ON CONFLICT (nyckel) DO UPDATE SET varde = EXCLUDED.varde, uppdaterad = NOW()
        """, (nyckel, varde))
    else:
        cur.execute(f"""
            INSERT OR REPLACE INTO {tabell} (nyckel, varde, uppdaterad)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (nyckel, varde))
    conn.commit()
    cur.close()
    conn.close()



def rensa_utgangna_remissvar():
    """Tar bort remissvar vars TTL har passerat."""
    conn = get_conn()
    cur = conn.cursor()
    if _USE_POSTGRES:
        cur.execute("""
            DELETE FROM gov_data.remissvar
            WHERE cache_utgar_vid IS NOT NULL
              AND cache_utgar_vid < NOW()
        """)
    else:
        cur.execute("""
            DELETE FROM remissvar
            WHERE cache_utgar_vid IS NOT NULL
              AND cache_utgar_vid < CURRENT_TIMESTAMP
        """)
    borttagna = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    if borttagna:
        _log.info(f"Rensade {borttagna} utgångna remissvar.")
    return borttagna


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    initiera_schema()
