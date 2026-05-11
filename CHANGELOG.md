# Ändringslogg

Alla viktiga ändringar i detta projekt dokumenteras här.
Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versionshanteringen följer [Semantic Versioning](https://semver.org/).

## [2.2.0] — 2026-05-11

### Fixat
- **`gov_search_beslut` datumfilter returnerade alltid 0 träffar:** `vecka_ar`/
  `vecka_nummer` var NULL i befintliga rader eftersom ALTER TABLE-migrationen
  lade till kolumnerna utan att populera dem. Åtgärd: idempotent datamigration i
  `db.initiera_schema()` som kör `UPDATE … SET vecka_ar/vecka_nummer = regexp_match(vecka_url, …)`
  för alla rader där `vecka_ar IS NULL`.
- **`gov_search_beslut` visade äldst data överst:** `ORDER BY id DESC` gav fel
  sortering eftersom API:et returnerar nyast data först (lägst id = nyast). Ändrat
  till `ORDER BY vecka_ar DESC NULLS LAST, vecka_nummer DESC NULLS LAST, id DESC`.
- **`_datum_till_veckonyckel` NameError i `gov_search_arendeforteckning`:** Funktionen
  var nästlad inuti `gov_search_beslut` men anropades från `gov_search_arendeforteckning`.
  Flyttad till modulnivå.
- **`_absolut_cache_sokvag` NameError i `gov_hamta_arendeforteckning`:** Ersatt med
  `os.path.join(_SCRIPT_DIR, os.getenv("PDF_CACHE_DIR", "pdf_cache"))`.
- **`pdf_lib` NameError i `gov_hamta_arendeforteckning`:** `import pdf_lib as _pdf_lib`
  tillagd inuti funktionen.
- **`gov_hamta_remissvar` saknade `batch_storlek`/`fortsatt_fran_index` i signaturen.**

### Tillagt
- **Ärendeförteckningar (pre-sept 2024):** `gov_data.arendeforteckning` +
  `arendeforteckning_chunks`, MCP-verktygen `gov_hamta_arendeforteckning` och
  `gov_search_arendeforteckning`.
- **Chunkning och embedding:** `_chunka_och_indexera_dokument`, `gov_indexera_bulk`,
  `indexera_bulk.py` med lingua-baserad språkfiltrering per chunk.
- Totalt 12 MCP-verktyg.

### Tekniskt
- Servernamnet i Claude Desktop-config bytt till `gov-dokument-v2` för att
  kringgå namnbaserad verktygscache i MCP-klienter.

## [2.1.0] — 2026-05-11

### Tillagt
- **Batch-mönster i `gov_hamta_remissvar`:** parametrarna `batch_storlek`
  (standard 15, max 50) och `fortsatt_fran_index` med `nasta_index` i svaret.
  Löser timeout-problem vid remisser med många bilagor (>15 remissvar).
- **Regeringsbeslut-synk:** `gov_data.beslut` + `gov_data.beslut_diarienummer`,
  `gov_search_beslut`, `gov_get_beslut_by_diarienummer`. Hämtar via
  `Filter/GetFilteredItems`-API:et med veckokolumner (`vecka_ar`, `vecka_nummer`).
- **Daglig synk utökad** med `synka_beslut()` i `03_synka_data.py`.


## [2.0.1] — 2026-05-06

### Fixat
- **Regression i `hamta_listor_lib.upsert_dokument()` (kritisk):** Fältnamnen
  som lästes från g0v.se:s JSON-svar var av misstag refaktorerade till
  ASCII-svenska (`publicerad`, `typer`, `avsandare`, `kategorier`, `genvagar`,
  `bilagor`) samtidigt med kolumnerna i v2.0.0. g0v.se levererar dessa fält
  på engelska (`published`, `updated`, `types`, `senders`, `categories`,
  `shortcuts`, `attachments`). Konsekvens: nya remisser/lagrådsremisser/
  förordningsmotiv osv. som synkades efter v2.0.0 fick `None` i flera fält
  — bilagor inkluderade, vilket bröt remissvar-flödet för nya remisser.
  Befintliga 11 384 dokument är intakta eftersom synket inte hunnit köra
  mot extern data efter migrationen. Konventionen är att ASCII-svenska
  bara gäller projektets egen kod och databas — externa API-fält behåller
  ursprungsformat.
- Lade till docstring i `upsert_dokument()` som tydligt påpekar
  fältnamnskonventionen för framtida refaktorer.

### Tillagt
- `gov_search`-resultat innehåller nu fälten `antal_bilagor` (int) och
  `har_remissvar` (bool). `har_remissvar = True` för remisser (typ_kod 2099)
  med fler än en bilaga — alltså remisser där det finns remissvar att
  hämta via `gov_hamta_remissvar`.
- `gov_search`-typmappning accepterar nu också `remissmissiv` (synonym
  för `remiss`) och `internationella-overenskommelser` (pluralform). Hjälper
  AI-klienter som råkar använda kodbasens dokumentationsterminologi.
- Förtydligad docstring för `gov_search` som pekar ut verktyget som det
  primära ingångssteget för att hitta remiss-URL inför `gov_hamta_remissvar`
  och `gov_list_remissinstanser`.

## [2.0.0] — 2026-05-06

### Brytande ändringar — databas och Python-API

**Databas-rename i schemat `gov_data`** — kräver migration via
`db/migration_v2_0_0.sql`. Skriptet är idempotent och säkert att köra om.

Tabeller:
- `documents` → `dokument`
- `sync_status` → `synkstatus`

Kolumner i `gov_data.dokument`:
- `published` → `publicerad`
- `updated` → `uppdaterad`
- `types` → `typer`
- `senders` → `avsandare`
- `categories` → `kategorier`
- `shortcuts` → `genvagar`
- `attachments` → `bilagor`

Kolumner i andra tabeller:
- `gov_data.document_chunks.document_id` → `dokument_id`
- `gov_data.remissvar.attachment_url` → `bilage_url`
- `gov_data.remissvar.cache_expires_at` → `cache_utgar_vid`
- `gov_data.beslut.diarienummer_raw` → `diarienummer_text`
- `gov_data.synkstatus.värde` → `varde`

**Python-identifierare** — 28 unika identifierare flyttade från svenska
tecken till ASCII-svenska enligt projektkonvention. Bland annat:
- `kör()` → `kor()` (huvudfunktion i pdf_lib, hamta_listor_lib, 03_synka_data)
- `sökväg` → `sokvag` (alla varianter, även halv-renamad `sokväg`)
- `värde` → `varde`
- `FÖRDRÖJNING` → `FORDROJNING`
- `avsändare_kod` → `avsandare_kod` (sannolik orsak till "verktyg saknas"-buggen i Claude Desktop)
- `_hämta_modell` → `_hamta_modell`
- `_hämta_pdf_on_demand` → `_hamta_pdf_vid_behov` (också engelska→svenska)
- `hämta_latest_updated` → `hamta_senast_uppdaterad` (också engelska→svenska)
- `hämta_dokument_för_bulk` → `hamta_dokument_for_bulk`
- `attachment_url` → `bilage_url` (variabel i pdf_lib)

### Tekniskt
- Lade till `db/migration_v2_0_0.sql` med PL/pgSQL-helperfunktioner
  `pg_temp.byt_tabell` och `pg_temp.byt_kolumn` som kontrollerar mot
  `information_schema` innan rename — gör skriptet idempotent.

## [Unreleased]

### Tillagt
- Initialt projekt: hämtning av JSON-listor från g0v.se
- Bulk-nedladdning av PDF:er för förordningsmotiv, remissmissiv och internationella överenskommelser
- MCP-server med 6 verktyg: gov_list_typer, gov_search, gov_get_document,
  gov_search_in_document, gov_search_beslut, gov_get_beslut_by_diarienummer
- Daglig synkronisering via 03_synka_data.py
- PostgreSQL-schema gov_data med pgvector-stöd, SQLite-fallback
