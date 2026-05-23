# Ändringslogg

Alla viktiga ändringar i detta projekt dokumenteras här.
Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versionshanteringen följer [Semantic Versioning](https://semver.org/).

## [3.1.0] — 2026-05-23

### Tillagt

- **Live-fallback i `gov_get_document`** — om URL:en saknas i lokal cache
  (t.ex. vid misslyckad daglig synk) görs ett automatiskt live-försök mot g0v.se.
  Rätt JSON-lista hämtas baserat på URL-prefix, matchande post hittas och
  upserteras i databasen. Framtida anrop och sökningar fungerar därefter
  utan ny live-hämtning.
- **`gov_search(sok_live=True)`** — ny parameter som vid tomma träffar i lokal
  databas söker live i relevanta g0v.se-listor och upserterar träffarna.
  Standardvärde False (bakåtkompatibelt).
- **Hjälpfunktioner** — `_hamta_fran_g0v_live()`, `_hamta_g0v_lista()`,
  `_G0V_SESSION`, `_URL_PREFIX_TILL_LISTA` för URL-prefix-till-lista-mappning.
- **In-memory-cache för g0v.se-listor** — `_G0V_LISTE_CACHE` med 5 minuters TTL
  reducerar nätverksanrop vid upprepade live-hämtningar inom samma serverprocess.
- **Förbättrade docstrings** — `gov_search`, `gov_get_document`,
  `gov_search_beslut`, `gov_hamta_remissvar`, `gov_list_remissinstanser`,
  `gov_search_remissvar`: cache-medvetenhet, arbetsflödet remiss→instanser→svar,
  vad `har_fulltext=False` innebär, korrekt beskrivning av bilagor[0] som
  remissmissivet (inte ett remissvar).
- **Modulbeskrivning uppdaterad** — arbetsflödena och live-fallbacken
  dokumenterade i filens inledande docstring.
- **FTS-tokenisering i `gov_search`** — query delas på whitespace och varje
  substantiellt ord AND-matchas individuellt med ILIKE (cache-grenen) respektive
  Python substring-test (live-grenen). Sökning på t.ex. `"2024:50 nätt jämnt"`
  träffar nu dokument där alla tre orden förekommer oberoende av varandra i titel
  eller sammanfattning. Stoppord (svenska småord) filtreras via `_STOPPORD`.
  Citationstecken runt hela query ger gammalt beteende — exakt frasmatchning.
- **`_STOPPORD` på modulnivå** — empiriskt vald stoppordslista (32 svenska
  småord) för juridiska och parlamentariska sökfrågor. Filtreras bort ur
  tokeniserad FTS-sökning i cache- och live-grenen. Kommenterad med notering
  om framtida tsvector+GIN-migration (se backlogg i stream-09-dokumentet).
- **Retry-strategi i `gov_search`-docstring** — explicit regel att vid noll-träff
  på flerordssökning med dokumentbeteckning (mönster YYYY:N eller YYYY/YY:N)
  ska nästa sökning göras med enbart beteckningen — aldrig genom att tappa
  beteckningen och söka på ämnesord. Förhindrar att AI-agenter felaktigt
  konkluderar att ett dokument saknas.

### Fixat

- **B1 — felklassificering av typ_kod vid live-upsert i `gov_search`:** live-träffar
  gruppas nu per faktisk typ_kod (via URL-prefix) och upserteras per grupp. Tidigare
  upserterades alla träffar med en enda typ_kod oavsett ursprungslista, vilket
  ledde till databasdataförstörelse vid blandade dokumenttyper.
- **B3 — inkonsekvent svarsstruktur cache vs live:** SQLite-grenen i live-sökvägen
  beräknar nu `antal_bilagor` och `har_remissvar` på samma sätt som
  `_rad_till_dict_dokument`. Fältet `kalla` borttaget (fanns bara i live-grenen).
- **Bg2 — fel returtyp vid noll-träff i `gov_search(sok_live=True)`:** returnerade
  tidigare `[{"info": "..."}]` (bryter kontraktet `list[dict]`). Returnerar nu `[]`
  så att kedjning med `gov_get_document` inte kraschar på saknad `url`-nyckel.
- **B4 — `avsandare_kod`-filter saknades i live-grenen:** filtret tillämpas nu
  även vid live-sökning, så att parameter-paritet med cache-grenen uppnås.
- **B5 — datumjämförelse på sträng-prefix:** `publicerad[:4] < str(year_from)` ersatt
  med `int(publicerad[:4]) < year_from` (med ValueError-hantering) för konsekvens
  med cache-grenens SQL-jämförelse.
- **Bg3 — sz-budgeten tillföll listan som kom först:** live-sökvägen bryter inte
  längre ur ytterloopen när `sz` uppnås. Alla listor genomsöks alltid; träffarna
  sorteras på `publicerad` och skärs till `sz` vid utskriften. Per-lista-cap
  `sz * 3` begränsar minnestillväxten utan att strypa sena listor.
- **B2 — felaktig docstring i `_hamta_fran_g0v_live`:** docstringen angav "returnerar
  ett dict" men funktionen returnerar en databasrad (tupel). Rättad.
- **K5 — driftsmanualsinstruktion i felmeddelande:** "Om synken nyligen misslyckats,
  kör 03_synka_data.py manuellt" borttaget ur det felmeddelande som returneras till
  AI-agenten/användaren.
- **K6 — `_hamta_g0v_lista` saknade User-Agent-kommentar:** docstringen nämner nu
  att User-Agent följer projektkonventionen för WAF-kompatibilitet.

### Förändrat

- **K1 — `import requests as _requests` flyttad till toppen:** importen låg tidigare
  mitt i filen (rad ~178). Nu placerad i imports-blocket högst upp, enligt PEP 8.
- **K2 — `from hamta_listor_lib import upsert_dokument` lyfts till modulnivå:**
  ersätter två separata lazy-imports i `_hamta_fran_g0v_live` och `gov_search`.
- **K3 — fyra olika `typ_kodmappning.get()`-defaults normaliserade:** alla kvarvarande
  anrop använder nu `get(typ.lower(), typ)` (okänd typ passerar igenom som-är).
  De övriga tre varianterna (`"2099"`, `""`, genomsläpp utan default) eliminerades
  i och med B1-/B3-fixarna.

## [3.0.0] — 2026-05-22

### Brytande ändringar

- **Extern SQL** — DDL flyttad ur `initiera_schema()` till `db/schema_postgres.sql`
  och `db/schema_sqlite.sql`. Möjliggör granskning och diff utan att köra Python.
- **Per-anrops-anslutning** — `db.py` exporterar nu `_ar_postgres()`, `_hamta_db()`,
  `_ph()` och `_prefix()` istället för `get_conn()` och `_USE_POSTGRES`. Alla
  anropare i `mcp_server.py`, `03_synka_data.py` och `hamta_listor_lib.py` uppdaterade.
- **Synkstatusnycklar** — `g0v_latest_updated` → `g0v_senast_uppdaterad`,
  `listor_senast_hämtade` → `listor_senast_hamtade`. Befintliga rader i
  `synkstatus`-tabellen behöver migreras (se backfill-instruktioner i
  `09-dokument-fran-regeringen-via-g0v_se.md`).
- **Env-variabelnamn** — `PYTHON_SOKVÄG` → `PYTHON_SOKVAG` (tar bort Ä).

### Fixat

- **Bugg 1 — HTTP-läge fail-open:** `BearerTokenMiddleware` tillät anrop utan
  autentisering när `MCP_API_KEY` var tom. Nu startar servern inte alls i HTTP-läge
  om `MCP_API_KEY` saknas (`SystemExit` med tydligt felmeddelande).
- **Bugg 2 — departement hamnade i `statsrad`:** `tolka_beslut_html()` placerade
  departementsnamn i `statsrad`-kolumnen när bara ett värde parsades ur HTML.
  Åtgärd: ny `_ar_departement()`-hjälpare som kontrollerar om värdet slutar på
  `departementet`/`beredningen` och placerar det i rätt kolumn.
- **Bugg 3 — `(pdf X kB)`-suffix och `"X av Y"`-prefix i remissinstansnamn:**
  `_extrahera_remissinstans()` regex utökad med `(\d+\s*av\s*\d+\s*)?` för att
  även ta bort ordningsmarkörer som `"3 av 3"` ur bilagenamnet.
- **Beg.6 (kvarstående) — `id` exponerades i `gov_search_beslut` och
  `gov_get_beslut_by_diarienummer`:** `id` borttaget ur SELECT-lista och returdict
  i båda verktygen. `b.id` behålls i `SELECT DISTINCT` för `ORDER BY`-stöd men
  exponeras inte i returstrukturen.

### Förbättrat

- **Beg.1 — prefix-detektion i `tolka_beslut_html()`:** Ny `_fixa_titel()`
  detekterar kända prefix (`"Regeringens proposition"`, `"Lagrådsremiss"` m.fl.)
  och infogar mellanslag om de saknas mot resterande titel.
- **Beg.2 — SQLite-schema kompletterat:** `beslut`-tabellen får `vecka_nummer`/`vecka_ar`,
  `arendeforteckning` och `arendeforteckning_chunks` läggs till.
- **Beg.3 — Språkfiltrering i `gov_hamta_remissvar`:** `_ar_svensk()`-filter nu
  applicerat vid chunkning av remissvar, precis som vid `gov_hamta_arendeforteckning`.
- **Beg.4 — NULL-säkert vecka-filter i `gov_search_beslut`:** `IS NOT NULL`-villkor
  för `vecka_ar`/`vecka_nummer`.
- **Beg.5 — Ny chunkning (800 tecken + 200 tecken överlapp):** `_chunka_text()`
  använder nu teckenbaserad gräns (800) med överlapp (200) istället för
  ordbaserad gräns (400 ord).
- **Beg.7 — Migrationsblock i `db/schema_*.sql`.**
- **K3 — `"fallback"`-terminologi borttagen** ur `README.md` och `config.example.env`.
- **K6 — Docstring-räknare uppdaterad** ("Nio verktyg" → "Tolv verktyg").
- **K7 — `PYTHON_SOKVAG` i `config.example.env`** (tog bort Ä).
- **K8 — Synkstatusnycklar** på ASCII-svenska genomgående.
- **K10 — `synk_daglig.sh`** tillagd för enkel daglig körning från terminal eller launchd.
- **`datetime.utcnow()` → `datetime.now(timezone.utc)`:**
  Fyra förekomster i `03_synka_data.py`, `pdf_lib.py` och `hamta_listor_lib.py`.
- **`use_postgres`-parameter borttagen ur hjälpfunktioner:**
  `spara_beslut`, `synka_beslut`, `synka_nya_pdf` (`03_synka_data.py`);
  `uppdatera_dokument_med_fulltext`, `behandla_ett_dokument`, `stada_pdf_cache`,
  `hamta_dokument_for_bulk`, `kor` (`pdf_lib.py`); `upsert_dokument`, `kor`
  (`hamta_listor_lib.py`). Alla anropar nu `db._ar_postgres()`, `db._ph()`,
  `db._prefix()` direkt.
- **`ORDER BY b.id` i `gov_get_beslut_by_diarienummer`:** Kronologisk sortering
  återställd (ersatte felaktig `ORDER BY b.titel`). Docstring uppdaterad.
- **NULL-guard i `gov_search_arendeforteckning` vecka-filter:**
  `af.vecka_ar IS NOT NULL AND af.vecka_nummer IS NOT NULL` tillagt för
  `from_date`- och `to_date`-grenarna.
- **`04_indexera_chunks.py`:** Ny standalone bulk-indexeringsskript som kör utan
  MCP-timeout och loggar hoppsatta dokument till `fel_indexering.log`.

## [2.3.1] — 2026-05-17

### Fixat
- **MCP-servern kraschade när PostgreSQL var nere vid uppstart:** `db.initiera_schema()`
  anropades utan try/except i `__main__`-blocket. När Postgres-containern råkade vara
  nere (t.ex. efter omstart av datorn innan Docker startat) dog hela processen direkt
  med `psycopg2.OperationalError: connection to server at "127.0.0.1", port 5432
  failed: Connection refused`, och Claude Desktop loggade "Server transport closed
  unexpectedly". Åtgärd: try/except runt anropet — fel loggas som varning men
  servern startar ändå. Verktygsanrop felar tills DB är uppe, men användaren slipper
  starta om Claude Desktop när containern startas i efterhand.

## [2.3.0] — 2026-05-15

### Tillagt
- **PDF-cache TTL och automatisk städning:** Pipeline raderar nu PDF-filen direkt
  efter lyckad extraktion till databasen i `behandla_ett_dokument()`,
  `gov_hamta_remissvar()` och `gov_hamta_arendeforteckning()`. OCR-tempfiler
  (`_ocr`-suffix) raderas också.
- **`stada_pdf_cache()`** i `pdf_lib.py`: städar upp kvarliggande PDF-filer
  där `fulltext_md IS NOT NULL` i databasen. Täcker tabellerna `dokument`,
  `remissvar` och `arendeforteckning`. Körs automatiskt i slutet av varje
  daglig synk via `03_synka_data.py`.
- **`PDF_CACHE_TTL_DAGAR`** i `.env` (standard: 1 dag): säkerhetsventil som
  bevarar filer yngre än angivet antal dagar även om fulltexten finns i DB.

## [2.2.1] — 2026-05-15

### Fixat
- **`--installera-schema` kraschade med NameError:** `installera_schema()`
  var definierad *efter* `if __name__ == "__main__":`-blocket — Python
  exekverar modulnivåkod uppifrån och ned, så funktionen fanns inte när den
  anropades. Åtgärd: funktionsdefinitionen flyttad till före `if __name__`-blocket.
- **launchd-jobb saknades:** Ovanstående bugg innebar att `--installera-schema`
  aldrig körts framgångsrikt. Jobbet installerat manuellt efter fix
  (`se.magnuskolsjo.mcp-gov-synk.plist`, kör dagligen kl 06:45). *(Omdöpt från `se.riksdag-ai.gov-dokument-synk` 2026-05-17)*

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

## [0.1.0] — 2026-04-xx (baslinje vid forsta GitHub-publicering)

### Tillagt
- Initialt projekt: hämtning av JSON-listor från g0v.se
- Bulk-nedladdning av PDF:er för förordningsmotiv, remissmissiv och internationella överenskommelser
- MCP-server med 6 verktyg: gov_list_typer, gov_search, gov_get_document,
  gov_search_in_document, gov_search_beslut, gov_get_beslut_by_diarienummer
- Daglig synkronisering via 03_synka_data.py
- PostgreSQL-schema gov_data med pgvector-stöd, SQLite som alternativt val
