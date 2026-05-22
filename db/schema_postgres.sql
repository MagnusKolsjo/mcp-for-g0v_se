-- schema_postgres.sql
-- Baslinjeschema for gov_data-schemat i PostgreSQL.
-- Kors mot schema_sqlite.sql for SQLite-varianten.
-- Alla satser ar idempotenta (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).

CREATE SCHEMA IF NOT EXISTS gov_data;
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- Tabell: dokument
-- ============================================================
CREATE TABLE IF NOT EXISTS gov_data.dokument (
    id                   SERIAL PRIMARY KEY,
    url                  TEXT UNIQUE NOT NULL,
    typ_kod              TEXT NOT NULL,
    titel                TEXT,
    sammanfattning       TEXT,
    publicerad           DATE,
    uppdaterad           DATE,
    typer                TEXT[],
    avsandare            TEXT[],
    kategorier           TEXT[],
    genvagar             JSONB,
    bilagor              JSONB,
    fulltext_md          TEXT,
    fulltext_hamtad_vid  TIMESTAMPTZ,
    pdf_sokvag           TEXT,
    indexerad_vid        TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- Tabell: document_chunks
-- ============================================================
CREATE TABLE IF NOT EXISTS gov_data.document_chunks (
    id           SERIAL PRIMARY KEY,
    dokument_id  INT REFERENCES gov_data.dokument(id) ON DELETE CASCADE,
    chunk_index  INT,
    chunk_text   TEXT,
    embedding    vector(768)
);

CREATE INDEX IF NOT EXISTS idx_gov_chunks_embedding
    ON gov_data.document_chunks
    USING ivfflat (embedding vector_cosine_ops);

-- ============================================================
-- Tabell: beslut
-- ============================================================
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
);

CREATE INDEX IF NOT EXISTS idx_beslut_vecka
    ON gov_data.beslut(vecka_ar, vecka_nummer);

-- ============================================================
-- Tabell: beslut_diarienummer
-- ============================================================
CREATE TABLE IF NOT EXISTS gov_data.beslut_diarienummer (
    id            SERIAL PRIMARY KEY,
    beslut_id     INT REFERENCES gov_data.beslut(id) ON DELETE CASCADE,
    diarienummer  TEXT NOT NULL,
    komplett      BOOLEAN
);

CREATE INDEX IF NOT EXISTS idx_beslut_dnr
    ON gov_data.beslut_diarienummer(diarienummer);

-- ============================================================
-- Tabell: remissvar
-- ============================================================
CREATE TABLE IF NOT EXISTS gov_data.remissvar (
    id                  SERIAL PRIMARY KEY,
    remiss_id           INT REFERENCES gov_data.dokument(id) ON DELETE CASCADE,
    remissinstans       TEXT NOT NULL,
    bilage_url          TEXT UNIQUE NOT NULL,
    fulltext_md         TEXT,
    fulltext_hamtad_vid TIMESTAMPTZ,
    cache_utgar_vid     TIMESTAMPTZ,
    pdf_sokvag          TEXT,
    indexerad_vid       TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- Tabell: remissvar_chunks
-- ============================================================
CREATE TABLE IF NOT EXISTS gov_data.remissvar_chunks (
    id            SERIAL PRIMARY KEY,
    remissvar_id  INT REFERENCES gov_data.remissvar(id) ON DELETE CASCADE,
    chunk_index   INT,
    chunk_text    TEXT,
    remissinstans TEXT NOT NULL,
    remiss_url    TEXT NOT NULL,
    embedding     vector(768)
);

CREATE INDEX IF NOT EXISTS idx_remissvar_chunks_embedding
    ON gov_data.remissvar_chunks
    USING ivfflat (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_remissvar_chunks_remiss
    ON gov_data.remissvar_chunks(remiss_url);

-- ============================================================
-- Tabell: arendeforteckning
-- ============================================================
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
);

CREATE INDEX IF NOT EXISTS idx_af_vecka
    ON gov_data.arendeforteckning(vecka_ar, vecka_nummer);

CREATE INDEX IF NOT EXISTS idx_af_departement
    ON gov_data.arendeforteckning(departement);

-- ============================================================
-- Tabell: arendeforteckning_chunks
-- ============================================================
CREATE TABLE IF NOT EXISTS gov_data.arendeforteckning_chunks (
    id                    SERIAL PRIMARY KEY,
    arendeforteckning_id  INT REFERENCES gov_data.arendeforteckning(id) ON DELETE CASCADE,
    chunk_index           INT,
    chunk_text            TEXT,
    embedding             vector(768)
);

CREATE INDEX IF NOT EXISTS idx_af_chunks_embedding
    ON gov_data.arendeforteckning_chunks
    USING ivfflat (embedding vector_cosine_ops);

-- ============================================================
-- Tabell: synkstatus
-- ============================================================
CREATE TABLE IF NOT EXISTS gov_data.synkstatus (
    nyckel     TEXT PRIMARY KEY,
    varde      TEXT,
    uppdaterad TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- Migrationsblock (lags till vid forsta GitHub-publicering)
-- Lagg alla ALTER TABLE IF NOT EXISTS-satser har, aldrig i bas-schemat ovan.
-- ============================================================
