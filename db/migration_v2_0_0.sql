-- ============================================================
-- stream-09-gov-dokument: Migration v2.0.0 — ASCII-svenska identifierare
-- ============================================================
-- Brytande migration. Renamar tabeller och kolumner i gov_data-schemat
-- så att Python-koden i stream-09 v2.0.0 kan köra mot databasen.
--
-- Idempotent: hjälpfunktionerna kontrollerar mot information_schema och
-- hoppar tysta över rename som redan är applicerade. Säker att köra om.
--
-- Förutsättning: Claude Desktop ska vara stängt så att MCP-servern inte
-- läser/skriver mot tabellerna under transaktionen.
--
-- Backup ska tas FÖRE körning:
--   pg_dump riksdagstryck > ~/MCP-Servers/_backups/<datum>/riksdagstryck_full.sql
-- ============================================================

\set ON_ERROR_STOP on

BEGIN;

-- ----------------------------------------------------------
-- Hjälpfunktioner (skapas i pg_temp — försvinner efter session)
-- ----------------------------------------------------------

CREATE OR REPLACE FUNCTION pg_temp.byt_tabell(
    p_schema TEXT, p_gammal TEXT, p_ny TEXT
) RETURNS VOID AS $func$
DECLARE
    finns_gammal BOOLEAN;
    finns_ny     BOOLEAN;
BEGIN
    SELECT EXISTS(
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = p_schema AND table_name = p_gammal
    ) INTO finns_gammal;
    SELECT EXISTS(
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = p_schema AND table_name = p_ny
    ) INTO finns_ny;

    IF finns_gammal AND NOT finns_ny THEN
        EXECUTE format('ALTER TABLE %I.%I RENAME TO %I', p_schema, p_gammal, p_ny);
        RAISE NOTICE 'Bytte tabell %.% -> %', p_schema, p_gammal, p_ny;
    ELSIF finns_ny AND NOT finns_gammal THEN
        RAISE NOTICE 'Tabell %.% -> % redan applicerad — hoppar', p_schema, p_gammal, p_ny;
    ELSIF NOT finns_ny AND NOT finns_gammal THEN
        RAISE EXCEPTION 'Varken tabell % eller % finns i schema %', p_gammal, p_ny, p_schema;
    ELSE
        RAISE EXCEPTION 'BÅDA tabellerna % och % finns i %, manuell utredning kravs', p_gammal, p_ny, p_schema;
    END IF;
END;
$func$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION pg_temp.byt_kolumn(
    p_schema TEXT, p_tabell TEXT, p_gammal TEXT, p_ny TEXT
) RETURNS VOID AS $func$
DECLARE
    finns_gammal BOOLEAN;
    finns_ny     BOOLEAN;
BEGIN
    SELECT EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = p_schema AND table_name = p_tabell AND column_name = p_gammal
    ) INTO finns_gammal;
    SELECT EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = p_schema AND table_name = p_tabell AND column_name = p_ny
    ) INTO finns_ny;

    IF finns_gammal AND NOT finns_ny THEN
        EXECUTE format('ALTER TABLE %I.%I RENAME COLUMN %I TO %I',
                       p_schema, p_tabell, p_gammal, p_ny);
        RAISE NOTICE 'Bytte kolumn %.%.% -> %', p_schema, p_tabell, p_gammal, p_ny;
    ELSIF finns_ny AND NOT finns_gammal THEN
        RAISE NOTICE 'Kolumn %.%.% -> % redan applicerad — hoppar', p_schema, p_tabell, p_gammal, p_ny;
    ELSIF NOT finns_ny AND NOT finns_gammal THEN
        RAISE EXCEPTION 'Varken kolumn % eller % finns i %.%', p_gammal, p_ny, p_schema, p_tabell;
    ELSE
        RAISE EXCEPTION 'BÅDA kolumnerna % och % finns i %.%, manuell utredning kravs',
                        p_gammal, p_ny, p_schema, p_tabell;
    END IF;
END;
$func$ LANGUAGE plpgsql;


-- ----------------------------------------------------------
-- 1) Tabellrenamn
-- ----------------------------------------------------------
SELECT pg_temp.byt_tabell('gov_data', 'documents',   'dokument');
SELECT pg_temp.byt_tabell('gov_data', 'sync_status', 'synkstatus');

-- document_chunks och remissvar_chunks behålls oförändrade — `chunks` är
-- vedertagen AI-vokabulär (samma princip som `embedding`).
-- beslut, beslut_diarienummer, remissvar och remissvar_chunks har redan
-- svenska tabellnamn — inget rename behövs.

-- ----------------------------------------------------------
-- 2) Kolumnrenamn i gov_data.dokument
-- ----------------------------------------------------------
SELECT pg_temp.byt_kolumn('gov_data', 'dokument', 'published',   'publicerad');
SELECT pg_temp.byt_kolumn('gov_data', 'dokument', 'updated',     'uppdaterad');
SELECT pg_temp.byt_kolumn('gov_data', 'dokument', 'types',       'typer');
SELECT pg_temp.byt_kolumn('gov_data', 'dokument', 'senders',     'avsandare');
SELECT pg_temp.byt_kolumn('gov_data', 'dokument', 'categories',  'kategorier');
SELECT pg_temp.byt_kolumn('gov_data', 'dokument', 'shortcuts',   'genvagar');
SELECT pg_temp.byt_kolumn('gov_data', 'dokument', 'attachments', 'bilagor');

-- ----------------------------------------------------------
-- 3) Kolumnrenamn i document_chunks (tabellnamnet behålls)
-- ----------------------------------------------------------
SELECT pg_temp.byt_kolumn('gov_data', 'document_chunks', 'document_id', 'dokument_id');

-- ----------------------------------------------------------
-- 4) Kolumnrenamn i gov_data.remissvar
-- ----------------------------------------------------------
SELECT pg_temp.byt_kolumn('gov_data', 'remissvar', 'attachment_url',   'bilage_url');
SELECT pg_temp.byt_kolumn('gov_data', 'remissvar', 'cache_expires_at', 'cache_utgar_vid');

-- ----------------------------------------------------------
-- 5) Kolumnrenamn i gov_data.beslut
-- ----------------------------------------------------------
SELECT pg_temp.byt_kolumn('gov_data', 'beslut', 'diarienummer_raw', 'diarienummer_text');

-- ----------------------------------------------------------
-- 6) Kolumnrenamn i gov_data.synkstatus
-- ----------------------------------------------------------
SELECT pg_temp.byt_kolumn('gov_data', 'synkstatus', 'värde', 'varde');

-- ----------------------------------------------------------
-- Klar — bekräfta vid commit
-- ----------------------------------------------------------
COMMIT;

-- Efterkontroll (kör utanför transaktionen):
-- \dt gov_data.*       ska visa: dokument, document_chunks, beslut,
--                       beslut_diarienummer, remissvar, remissvar_chunks,
--                       synkstatus
-- \d gov_data.dokument ska visa kolumner: id, url, typ_kod, titel,
--                       sammanfattning, publicerad, uppdaterad, typer,
--                       avsandare, kategorier, genvagar, bilagor,
--                       fulltext_md, fulltext_hamtad_vid, pdf_sokvag,
--                       indexerad_vid
