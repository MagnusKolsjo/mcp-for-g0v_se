# mcp-for-g0v_se

MCP-server som ger AI-verktyg tillgång till svenska regeringsdokument via
[g0v.se](https://g0v.se) och [regeringen.se](https://www.regeringen.se).

Täcker lagrådsremisser, remissmissiv, förordningsmotiv, internationella
överenskommelser, kommenterade dagordningar och regeringsbeslut — från
1990-talet och framåt.

## Verktyg

| Verktyg | Beskrivning |
|---|---|
| `gov_search` | Söker i dokumentmetadata (typ, datum, avsändare, fritextmatch) |
| `gov_get_document` | Hämtar fulltext för ett specifikt dokument via URL |
| `gov_search_in_document` | Semantisk sökning i indexerade dokuments chunks |
| `gov_list_typer` | Listar tillgängliga dokumenttyper och deras koder |
| `gov_indexera_bulk` | Indexerar och embeddar dokument i omgångar |
| `gov_hamta_remissvar` | Laddar ned och cachar remissvar för en remiss |
| `gov_list_remissinstanser` | Listar remissinstanser för en remiss |
| `gov_search_remissvar` | Semantisk sökning i remissvar |
| `gov_search_beslut` | Söker i regeringsbeslut (sept 2024–) med datumfilter |
| `gov_get_beslut_by_diarienummer` | Hämtar beslut kopplade till ett diarienummer |
| `gov_hamta_arendeforteckning` | Hämtar och indexerar veckoförteckning (pre-sept 2024) |
| `gov_search_arendeforteckning` | Semantisk sökning i ärendeförteckningar |

## Krav

- Python 3.11+
- PostgreSQL med pgvector-tillägget (rekommenderas) eller SQLite (begränsat — semantisk sökning inaktiveras)
- Tesseract OCR för bildbaserade PDF:er: `brew install tesseract tesseract-lang`
- Internetåtkomst mot regeringen.se och g0v.se

## Installation

```bash
git clone https://github.com/MagnusKolsjo/mcp-for-g0v_se.git
cd mcp-for-g0v_se
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt
cp config.example.env .env
# Redigera .env med din databasanslutning
```

## Konfiguration

Kopiera `config.example.env` till `.env` och fyll i:

```env
# PostgreSQL (rekommenderas — krävs för semantisk sökning)
DATABASE_URL=postgresql://anvandare:losenord@localhost:5432/riksdag

# SQLite — utan pgvector (semantisk sökning inaktiveras)
# DATABASE_URL=sqlite:///gov_cache.db

# Transportläge för MCP-servern
MCP_TRANSPORT=stdio
```

## Initialisering och synkronisering

```bash
# Steg 1: Hämta dokumentlistor från g0v.se och initiera databasen
.venv/bin/python3 01_hamta_listor.py

# Steg 2: Ladda ned PDF:er för bulk-dokumenttyper (tar tid — kan köras i bakgrunden)
.venv/bin/python3 02_initial_bulk.py

# Steg 3: Daglig synkronisering (kör manuellt eller schemalägg)
.venv/bin/python3 03_synka_data.py

# Installera automatisk schemaläggning via cron eller launchd
.venv/bin/python3 03_synka_data.py --installera-schema
```

## MCP-konfiguration

Lägg till i din MCP-klient (t.ex. Claude Desktop, `claude_desktop_config.json`):

```json
"gov-dokument": {
  "command": "/sökväg/till/mcp-for-g0v_se/.venv/bin/python3",
  "args": ["/sökväg/till/mcp-for-g0v_se/mcp_server.py"],
  "cwd": "/sökväg/till/mcp-for-g0v_se"
}
```

## Licens

AGPLv3 — se [LICENSE](LICENSE).
