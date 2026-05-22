-- schema_sqlite.sql
-- Baslinjeschema for gov_data-schemat i SQLite.
-- Kors mot schema_postgres.sql for PostgreSQL-varianten.
-- SQLite stoder inte vektorsökning (pgvector) eller schemanamn.
-- Alla satser ar idempotenta (IF NOT EXISTS).

-- ============================================================
-- Tabell: dokument
-- ============================================================
CREATE TABLE IF NOT EXISTS dokument (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    url                  TEXT UNIQUE NOT NULL,
    typ_kod              TEXT NOT NULL,
    titel                TEXT,
    sammanfattning       TEXT,
    publicerad           TEXT,
    uppdaterad           TEXT,
    typer                TEXT,
    avsandare            TEXT,
    kategorier           TEXT,
    genvagar             TEXT,
    bilagor              TEXT,
    fulltext_md          TEXT,
    fulltext_hamtad_vid  TEXT,
    pdf_sokvag           TEXT,
    indexerad_vid        TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- Tabell: beslut
-- ============================================================
CREATE TABLE IF NOT EXISTS beslut (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    titel                     TEXT,
    regeringsarendenummer     TEXT,
    diarienummer_text         TEXT,
    ansvarig_chefstjansteman  TEXT,
    vecka_url                 TEXT,
    vecka_nummer              INT,
    vecka_ar                  INT,
    statsrad                  TEXT,
    departement               TEXT,
    indexerad_vid             TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_beslut_vecka
    ON beslut(vecka_ar, vecka_nummer);

-- ============================================================
-- Tabell: beslut_diarienummer
-- ============================================================
CREATE TABLE IF NOT EXISTS beslut_diarienummer (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    beslut_id     INTEGER REFERENCES beslut(id) ON DELETE CASCADE,
    diarienummer  TEXT NOT NULL,
    komplett      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_beslut_dnr
    ON beslut_diarienummer(diarienummer);

-- ============================================================
-- Tabell: remissvar
-- ============================================================
CREATE TABLE IF NOT EXISTS remissvar (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    remiss_id           INTEGER REFERENCES dokument(id) ON DELETE CASCADE,
    remissinstans       TEXT NOT NULL,
    bilage_url          TEXT UNIQUE NOT NULL,
    fulltext_md         TEXT,
    fulltext_hamtad_vid TEXT,
    cache_utgar_vid     TEXT,
    pdf_sokvag          TEXT,
    indexerad_vid       TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- Tabell: arendeforteckning
-- ============================================================
CREATE TABLE IF NOT EXISTS arendeforteckning (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vecka_sida_url  TEXT NOT NULL,
    vecka_nummer    INT,
    vecka_ar        INT,
    datum           TEXT,
    departement     TEXT,
    pdf_url         TEXT UNIQUE NOT NULL,
    pdf_sokvag      TEXT,
    fulltext_md     TEXT,
    indexerad_vid   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_af_vecka
    ON arendeforteckning(vecka_ar, vecka_nummer);

CREATE INDEX IF NOT EXISTS idx_af_departement
    ON arendeforteckning(departement);

-- ============================================================
-- Tabell: synkstatus
-- ============================================================
CREATE TABLE IF NOT EXISTS synkstatus (
    nyckel     TEXT PRIMARY KEY,
    varde      TEXT,
    uppdaterad TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- Migrationsblock (lags till vid forsta GitHub-publicering)
-- Lagg alla satser har, aldrig i bas-schemat ovan.
-- ============================================================
