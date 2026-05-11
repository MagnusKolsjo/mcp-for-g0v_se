"""
mcp_server.py — MCP-server för regeringsdokument och regeringsbeslut.

Nio verktyg:
  gov_list_typer               — Lista dokumenttyper med antal poster
  gov_search                   — Sök i metadata för alla dokumenttyper
  gov_get_document             — Hämta ett dokument (fulltext on-demand för icke-bulk-typer)
  gov_search_in_document       — Semantisk sökning inom ett dokument (kräver PostgreSQL)
  gov_indexera_bulk            — Bulk-chunkning och embedding för redan nedladdade dokument
  gov_hamta_arendeforteckning  — Hämtar och indexerar ärendeförtecknings-PDF:er on-demand (pre-sept 2024)
  gov_search_arendeforteckning — Semantisk sökning i indexerade ärendeförteckningar
  gov_search_beslut            — Sök i regeringsbeslut (Filter-API)
  gov_get_beslut_by_diarienummer — Hämta alla beslut kopplade till ett diarienummer
  gov_hamta_remissvar          — Ladda ned och cacha remissvar för en remiss (explicit trigger)
  gov_list_remissinstanser     — Lista remissinstanser med cachestatus
  gov_search_remissvar         — Semantisk sökning i remissvar

Transport styrs via MCP_TRANSPORT i .env: stdio (standard) eller http.
"""
import os
import json
from pathlib import Path
import re
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import db

load_dotenv()

log = logging.getLogger(__name__)

_SCRIPT_DIR   = Path(__file__).parent

MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
MCP_HOST      = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT      = int(os.getenv("MCP_PORT", "8009"))
MCP_API_KEY   = os.getenv("MCP_API_KEY", "")
REMISSVAR_TTL_DAYS = int(os.getenv("REMISSVAR_CACHE_TTL_DAYS", "365"))

# Dokumenttypers visningsnamn
TYP_NAMN = {
    "2085": "Lagrådsremiss",
    "2099": "Remissmissiv",
    "2098": "Kommenterad dagordning",
    "1326": "Förordningsmotiv",
    "1332": "Internationell överenskommelse",
}

# Typer med fulltext (bulk-indexerade eller on-demand)
# Konfigurerbara via .env — kommaseparerade typkoder
BULK_TYPER      = set(os.getenv("BULK_TYPER",     "1326,2099,1332").split(","))
ON_DEMAND_TYPER = set(os.getenv("ON_DEMAND_TYPER", "2085,2098").split(","))
ARENDEFORTECKNING_AKTIV       = os.getenv("ARENDEFORTECKNING_AKTIV", "true").lower() == "true"
ARENDEFORTECKNING_CACHE_TTL   = int(os.getenv("ARENDEFORTECKNING_CACHE_TTL_DAYS", "0"))

# Lazy-laddad embeddingmodell
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
    """Lazy-laddad lingua-språkdetektor. Returnerar None om lingua saknas."""
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
        log.info("Lingua-språkdetektor laddad")
    except ImportError:
        log.warning("lingua-language-detector ej installerat — språkfiltrering inaktiv")
        _detektor = False  # Markör: försökt men saknas
    return _detektor


def _ar_svensk(text: str) -> bool:
    """
    Returnerar True om texten bedöms vara på svenska, eller om detektorn saknas.
    Hoppar över för korta chunks (< 80 tecken) — för osäkra att bedöma.
    """
    if len(text.strip()) < 80:
        return True  # Behåll korta chunks utan filtrering
    detektor = _hamta_detektor()
    if not detektor:
        return True  # lingua saknas — filtrera inte
    try:
        from lingua import Language
        sprak = detektor.detect_language_of(text)
        return sprak == Language.SWEDISH
    except Exception:
        return True  # Vid fel — behåll chunken


mcp = FastMCP("gov-dokument")


# ---------------------------------------------------------------------------
# Hjälpfunktioner
# ---------------------------------------------------------------------------

def _datum_till_veckonyckel(datum_str: str):
    """Konverterar YYYY-MM-DD till veckonyckel vecka_ar*100 + vecka_nummer."""
    try:
        import datetime
        d = datetime.date.fromisoformat(datum_str)
        iso = d.isocalendar()
        return iso[0] * 100 + iso[1]   # (year, week, weekday)
    except Exception:
        return None

def _use_postgres() -> bool:
    return db.DATABASE_URL.startswith("postgresql")


def _rad_till_dict_dokument(rad) -> dict:
    """Konverterar en databasrad till ett dokumentobjekt.

    Tilläggsfält som beräknas i Python:
      - antal_bilagor: antal PDF-bilagor på dokumentet
      - har_remissvar: True för remisser (typ_kod 2099) med fler än en bilaga
        (första bilagan är remissmissivet, övriga är remissvar)
    """
    bilagor = json.loads(rad[8]) if isinstance(rad[8], str) else (rad[8] or [])
    typ_kod = rad[2]
    antal_bilagor = len(bilagor)
    har_remissvar = typ_kod == "2099" and antal_bilagor > 1
    return {
        "id":            rad[0],
        "url":           rad[1],
        "typ":           TYP_NAMN.get(typ_kod, typ_kod),
        "typ_kod":       typ_kod,
        "titel":         rad[3],
        "sammanfattning": rad[4],
        "publicerad":     str(rad[5]) if rad[5] else None,
        "avsandare":       rad[6],
        "genvagar":     json.loads(rad[7]) if isinstance(rad[7], str) else (rad[7] or []),
        "bilagor":   bilagor,
        "antal_bilagor": antal_bilagor,
        "har_remissvar": har_remissvar,
        "har_fulltext":  rad[9] is not None,
    }


def _hamta_pdf_vid_behov(doc_id: int, doc_url: str, bilagor) -> Optional[str]:
    """
    Hämtar och indexerar PDF on-demand för icke-bulk-typer.
    Returnerar extraherad text eller None.
    """
    from pdf_lib import behandla_ett_dokument
    conn = db.get_conn()
    use_pg = _use_postgres()

    if isinstance(bilagor, str):
        bilagor = json.loads(bilagor)

    doc = {
        "id": doc_id,
        "url": doc_url,
        "typ_kod": "",
        "titel": "",
        "bilagor": bilagor,
    }
    status = behandla_ett_dokument(doc, conn, use_pg)
    conn.close()

    if status.startswith("OK"):
        # Hämta den nu sparade fulltexten
        conn2 = db.get_conn()
        cur = conn2.cursor()
        tabell = "gov_data.dokument" if use_pg else "dokument"
        plats  = "%s" if use_pg else "?"
        cur.execute(f"SELECT fulltext_md FROM {tabell} WHERE id = {plats}", (doc_id,))
        rad = cur.fetchone()
        cur.close()
        conn2.close()
        return rad[0] if rad else None
    return None


# ---------------------------------------------------------------------------
# Verktyg
# ---------------------------------------------------------------------------

@mcp.tool()
def gov_list_typer() -> list[dict]:
    """
    Listar tillgängliga dokumenttyper med antal dokument och indexeringsstatus.

    Returnerar en lista med: typ, typ_kod, antal, antal_med_fulltext, indexeringsstrategi.
    """
    conn = db.get_conn()
    cur  = conn.cursor()
    tabell = "gov_data.dokument" if _use_postgres() else "dokument"

    cur.execute(f"""
        SELECT typ_kod,
               COUNT(*) AS antal,
               COUNT(fulltext_md) AS med_fulltext
        FROM {tabell}
        GROUP BY typ_kod
        ORDER BY antal DESC
    """)

    result = []
    for rad in cur.fetchall():
        typ_kod, antal, med_fulltext = rad
        if typ_kod in BULK_TYPER:
            strategi = "fulltext — bulk-indexerad"
        elif typ_kod in ON_DEMAND_TYPER:
            strategi = "fulltext — hämtas on-demand"
        else:
            strategi = "enbart metadata"
        result.append({
            "typ":                TYP_NAMN.get(typ_kod, typ_kod),
            "typ_kod":            typ_kod,
            "antal":              antal,
            "antal_med_fulltext": med_fulltext,
            "indexeringsstrategi": strategi,
        })

    cur.close()
    conn.close()
    return result


@mcp.tool()
def gov_search(
    query: str = "",
    typ: str = "",
    year_from: int = 0,
    year_to: int = 0,
    avsandare_kod: str = "",
    sz: int = 20,
) -> list[dict]:
    """
    Söker i metadata för alla regeringsdokument (lagrådsremisser, remisser,
    förordningsmotiv, internationella överenskommelser, kommenterade dagordningar).

    Detta är det primära ingångsverktyget för att hitta ett dokument utifrån
    titel, SOU-beteckning eller ämnesord. Använd det här verktyget när du vet
    titel/ämne men inte URL — t.ex. innan ett anrop till gov_hamta_remissvar
    eller gov_list_remissinstanser som kräver känd remiss-URL.

    Args:
        query:         Fritextsökning i titel och sammanfattning. Sök på SOU-beteckning
                       (t.ex. "2025:79") eller ämnesord (t.ex. "cybersäkerhet").
        typ:           Filtrera på dokumenttyp. Accepterade värden: lagradsremiss,
                       remiss, remissmissiv (synonym för remiss), kommenterad-dagordning,
                       forordningsmotiv, internationell-overenskommelse,
                       internationella-overenskommelser.
        year_from:     Tidigaste publiceringsår (inklusivt).
        year_to:       Senaste publiceringsår (inklusivt).
        avsandare_kod: Filtrera på avsändarkod (t.ex. "1287" för Justitiedepartementet).
        sz:            Antal resultat (max 100).

    Returnerar lista med: titel, typ, publicerad, sammanfattning, url, har_fulltext,
    bilagor, antal_bilagor, har_remissvar. Fältet har_remissvar = True för remisser
    med fler än en bilaga (alltså remisser där det finns remissvar att hämta).
    """
    typ_kodmappning = {
        "lagradsremiss":                  "2085",
        "remiss":                         "2099",
        "remissmissiv":                   "2099",  # synonym — kodbasen anvander båda termerna
        "kommenterad-dagordning":         "2098",
        "forordningsmotiv":               "1326",
        "internationell-overenskommelse": "1332",
        "internationella-overenskommelser": "1332",  # synonym — pluralform
    }
    use_pg = _use_postgres()
    conn   = db.get_conn()
    cur    = conn.cursor()
    tabell = "gov_data.dokument" if use_pg else "dokument"
    plats  = "%s" if use_pg else "?"

    villkor = ["1=1"]
    params  = []

    if typ:
        kod = typ_kodmappning.get(typ.lower(), typ)
        villkor.append(f"typ_kod = {plats}")
        params.append(kod)

    if query:
        if use_pg:
            villkor.append(f"(titel ILIKE {plats} OR sammanfattning ILIKE {plats})")
            params.extend([f"%{query}%", f"%{query}%"])
        else:
            villkor.append(f"(titel LIKE {plats} OR sammanfattning LIKE {plats})")
            params.extend([f"%{query}%", f"%{query}%"])

    if year_from:
        villkor.append(f"publicerad >= {plats}")
        params.append(f"{year_from}-01-01")

    if year_to:
        villkor.append(f"publicerad <= {plats}")
        params.append(f"{year_to}-12-31")

    if avsandare_kod:
        if use_pg:
            villkor.append(f"{plats} = ANY(avsandare)")
        else:
            villkor.append(f"avsandare LIKE {plats}")
            avsandare_kod = f"%{avsandare_kod}%"
        params.append(avsandare_kod)

    sz = min(sz, 100)
    sql = f"""
        SELECT id, url, typ_kod, titel, sammanfattning, publicerad,
               avsandare, genvagar, bilagor, fulltext_md
        FROM {tabell}
        WHERE {' AND '.join(villkor)}
        ORDER BY publicerad DESC {'NULLS LAST' if use_pg else ''}
        LIMIT {plats}
    """
    params.append(sz)
    cur.execute(sql, params)

    result = [_rad_till_dict_dokument(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return result


@mcp.tool()
def gov_get_document(url: str, hamta_fulltext: bool = True) -> dict:
    """
    Hämtar ett enskilt dokument med fulltext.

    För bulk-typer (förordningsmotiv, remissmissiv, internationella överenskommelser)
    returneras sparad fulltext direkt. För on-demand-typer (lagrådsremisser,
    kommenterade dagordningar) hämtas och cachas PDF:en om den inte redan finns.

    Args:
        url:            g0v.se-URL för dokumentet (t.ex. /rattsliga-dokument/lagradsremiss/...).
        hamta_fulltext: Om True hämtas PDF on-demand om fulltext saknas (standard: True).

    Returnerar: titel, typ, publicerad, sammanfattning, fulltext_md (eller None),
                genvagar, bilagor (direktlänkar till regeringen.se).
    """
    use_pg = _use_postgres()
    conn   = db.get_conn()
    cur    = conn.cursor()
    tabell = "gov_data.dokument" if use_pg else "dokument"
    plats  = "%s" if use_pg else "?"

    cur.execute(f"""
        SELECT id, url, typ_kod, titel, sammanfattning, publicerad,
               avsandare, genvagar, bilagor, fulltext_md
        FROM {tabell}
        WHERE url = {plats}
    """, (url,))

    rad = cur.fetchone()
    cur.close()
    conn.close()

    if not rad:
        return {"fel": f"Dokument med URL '{url}' hittades inte i databasen."}

    doc = _rad_till_dict_dokument(rad)
    doc_id = rad[0]
    fulltext = rad[9]
    bilagor = rad[8]

    # On-demand-hämtning för typer utan bulk-indexering
    if fulltext is None and hamta_fulltext and doc["typ_kod"] in ON_DEMAND_TYPER:
        log.info(f"On-demand PDF-hämtning för: {doc['titel'][:60]}")
        fulltext = _hamta_pdf_vid_behov(doc_id, url, bilagor)

    doc["fulltext_md"] = fulltext

    # Chunka och indexera om fulltext finns och PostgreSQL används
    if fulltext and _use_postgres() and doc_id:
        try:
            conn2 = db.get_conn()
            _chunka_och_indexera_dokument(doc_id, fulltext, conn2)
            conn2.close()
        except Exception as e:
            log.warning(f"Chunkning misslyckades för {url}: {e}")

    return doc


@mcp.tool()
def gov_search_in_document(url: str, query: str, top_k: int = 5) -> list[dict]:
    """
    Semantisk sökning inom ett enskilt dokument (kräver PostgreSQL med pgvector).

    Söker i tidigare indexerade textstycken för dokumentet. Om dokumentet
    inte har chunks ännu, se till att gov_get_document anropats först.

    Args:
        url:    g0v.se-URL för dokumentet.
        query:  Sökfråga på svenska.
        top_k:  Antal relevanta stycken att returnera (standard: 5).

    Returnerar lista med chunk_text och relevanspoäng (cosinuslikhet).
    """
    if not _use_postgres():
        return [{"fel": "Semantisk sökning kräver PostgreSQL med pgvector. SQLite stöds inte."}]

    modell = _hamta_modell()
    fraga_vektor = modell.encode(query).tolist()

    conn = db.get_conn()
    cur  = conn.cursor()

    cur.execute("""
        SELECT dc.chunk_text,
               1 - (dc.embedding <=> %s::vector) AS relevans
        FROM gov_data.document_chunks dc
        JOIN gov_data.dokument d ON d.id = dc.dokument_id
        WHERE d.url = %s
        ORDER BY dc.embedding <=> %s::vector
        LIMIT %s
    """, (fraga_vektor, url, fraga_vektor, top_k))

    result = [
        {"chunk_text": r[0], "relevans": round(float(r[1]), 4)}
        for r in cur.fetchall()
    ]

    cur.close()
    conn.close()

    if not result:
        return [{"info": "Inga chunks hittades. Anropa gov_get_document först för att indexera dokumentet."}]

    return result


@mcp.tool()
def gov_indexera_bulk(
    batch_storlek: int = 10,
    fortsatt_fran_index: int = 0,
) -> dict:
    """
    Chunkar och embeddar redan nedladdade dokument i bulk.

    Behandlar dokument som har fulltext_md men saknar chunks i document_chunks.
    Använd batch_storlek och fortsatt_fran_index för att köra i omgångar och
    undvika timeout.

    Args:
        batch_storlek:       Antal dokument per anrop (standard 10, max 25).
        fortsatt_fran_index: Startindex för detta anrop (0-baserat, från nasta_index).

    Returnerar: totalt_kvar, detta_batch, indexerade_chunks, nasta_index (null om klart).
    """
    if not _use_postgres():
        return {"fel": "Bulk-indexering kräver PostgreSQL med pgvector."}

    conn = db.get_conn()
    cur  = conn.cursor()

    batch_storlek = max(1, min(batch_storlek, 25))
    start = max(0, fortsatt_fran_index)

    # Hämta dokument med fulltext men utan chunks
    cur.execute("""
        SELECT d.id, d.url
        FROM gov_data.dokument d
        WHERE d.fulltext_md IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM gov_data.document_chunks dc
              WHERE dc.dokument_id = d.id
          )
        ORDER BY d.id
        OFFSET %s
    """, (start,))

    alla = cur.fetchall()
    totalt_kvar  = len(alla)
    detta_batch  = alla[:batch_storlek]
    nasta_index  = start + batch_storlek if len(alla) > batch_storlek else None

    indexerade_chunks = 0
    for dok_id, url in detta_batch:
        cur2 = conn.cursor()
        cur2.execute("SELECT fulltext_md FROM gov_data.dokument WHERE id = %s", (dok_id,))
        rad = cur2.fetchone()
        cur2.close()
        if rad and rad[0]:
            try:
                indexerade_chunks += _chunka_och_indexera_dokument(dok_id, rad[0], conn)
            except Exception as e:
                log.warning(f"Chunkning misslyckades för {url}: {e}")

    cur.close()
    conn.close()

    return {
        "totalt_kvar":       totalt_kvar,
        "detta_batch":       len(detta_batch),
        "indexerade_chunks": indexerade_chunks,
        "nasta_index":       nasta_index,
    }


@mcp.tool()
def gov_search_beslut(
    query: str = "",
    from_date: str = "",
    to_date: str = "",
    departement: str = "",
    statsrad: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """
    Söker i regeringsbeslut sedan september 2024.

    Datan hämtas från regeringen.se:s Filter/GetFilteredItems-API och täcker
    alla typer av beslut: lagrådsremisser, propositioner, regleringsbrev,
    EU-rådsärenden m.fl.

    Args:
        query:       Fritextsökning i beslutstitel.
        from_date:   Startdatum YYYY-MM-DD.
        to_date:     Slutdatum YYYY-MM-DD.
        departement: Filtrera på departement (partiell matchning, t.ex. "Justitie").
        statsrad:    Filtrera på statsråd (partiell matchning).
        page:        Sidnummer (1-baserat).
        page_size:   Antal per sida (max 100).

    Returnerar: totalt antal träffar, sida, poster (titel, diarienummer, statsråd, departement, vecka_url).
    """
    use_pg = _use_postgres()
    conn   = db.get_conn()
    cur    = conn.cursor()
    tabell = "gov_data.beslut" if use_pg else "beslut"
    plats  = "%s" if use_pg else "?"

    villkor = ["1=1"]
    params  = []

    if query:
        if use_pg:
            villkor.append(f"titel ILIKE {plats}")
        else:
            villkor.append(f"titel LIKE {plats}")
        params.append(f"%{query}%")

    if from_date:
        nyckel = _datum_till_veckonyckel(from_date)
        if nyckel is not None:
            villkor.append(f"(vecka_ar * 100 + vecka_nummer) >= {plats}")
            params.append(nyckel)

    if to_date:
        nyckel = _datum_till_veckonyckel(to_date)
        if nyckel is not None:
            villkor.append(f"(vecka_ar * 100 + vecka_nummer) <= {plats}")
            params.append(nyckel)

    if departement:
        if use_pg:
            villkor.append(f"departement ILIKE {plats}")
        else:
            villkor.append(f"departement LIKE {plats}")
        params.append(f"%{departement}%")

    if statsrad:
        if use_pg:
            villkor.append(f"statsrad ILIKE {plats}")
        else:
            villkor.append(f"statsrad LIKE {plats}")
        params.append(f"%{statsrad}%")

    # Räkna totalt
    cur.execute(
        f"SELECT COUNT(*) FROM {tabell} WHERE {' AND '.join(villkor)}",
        params
    )
    totalt = cur.fetchone()[0]

    # Hämta sida
    page_size = min(page_size, 100)
    offset    = (page - 1) * page_size
    params_sida = params + [page_size, offset]

    cur.execute(f"""
        SELECT id, titel, regeringsarendenummer, diarienummer_text,
               statsrad, departement, vecka_url
        FROM {tabell}
        WHERE {' AND '.join(villkor)}
        ORDER BY vecka_ar DESC NULLS LAST, vecka_nummer DESC NULLS LAST, id DESC
        LIMIT {plats} OFFSET {plats}
    """, params_sida)

    poster = [
        {
            "id":                    r[0],
            "titel":                 r[1],
            "regeringsarendenummer": r[2],
            "diarienummer":          r[3],
            "statsrad":              r[4],
            "departement":           r[5],
            "vecka_url":             r[6],
        }
        for r in cur.fetchall()
    ]

    cur.close()
    conn.close()

    return {
        "totalt":    totalt,
        "sida":      page,
        "per_sida":  page_size,
        "poster":    poster,
    }


@mcp.tool()
def gov_get_beslut_by_diarienummer(diarienummer: str) -> list[dict]:
    """
    Hämtar alla regeringsbeslut kopplade till ett specifikt diarienummer.

    Diarienumret är den identifierare som knyter samman dokument i
    lagstiftningskedjan (remiss → lagrådsremiss → proposition).
    Söker på exakt matchning och prefix (t.ex. "Ju2023/00712" matchar
    även "Ju2023/00712, Ju2025/01097").

    Args:
        diarienummer: Diarienummer att söka på, t.ex. "Ju2023/00712".

    Returnerar lista med beslut sorterade på id (kronologisk ordning).
    """
    use_pg = _use_postgres()
    conn   = db.get_conn()
    cur    = conn.cursor()
    tabell_b = "gov_data.beslut" if use_pg else "beslut"
    tabell_d = "gov_data.beslut_diarienummer" if use_pg else "beslut_diarienummer"
    plats    = "%s" if use_pg else "?"

    cur.execute(f"""
        SELECT DISTINCT b.id, b.titel, b.regeringsarendenummer, b.diarienummer_text,
                        b.statsrad, b.departement, b.vecka_url
        FROM {tabell_b} b
        JOIN {tabell_d} d ON d.beslut_id = b.id
        WHERE d.diarienummer = {plats}
           OR d.diarienummer LIKE {plats}
        ORDER BY b.id
    """, (diarienummer, f"{diarienummer}%"))

    result = [
        {
            "id":                    r[0],
            "titel":                 r[1],
            "regeringsarendenummer": r[2],
            "diarienummer":          r[3],
            "statsrad":              r[4],
            "departement":           r[5],
            "vecka_url":             r[6],
        }
        for r in cur.fetchall()
    ]

    cur.close()
    conn.close()
    return result



# ---------------------------------------------------------------------------
# Hjälpfunktioner — remissvar
# ---------------------------------------------------------------------------

def _extrahera_remissinstans(bilage_namn: str) -> str:
    """Extraherar remissinstansens namn ur bilagenamnet.
    "Tillväxtverket (pdf 376 kB)" → "Tillväxtverket"
    """
    return re.sub(r'\s*\(pdf[^)]*\)\s*$', '', bilage_namn, flags=re.IGNORECASE).strip()


def _chunka_text(text: str, max_ord: int = 400) -> list[str]:
    """Delar upp text i chunks om max max_ord ord, längs styckegränser."""
    stycken = [s.strip() for s in text.split('\n\n') if s.strip()]
    chunks, aktuell, ord_raknare = [], [], 0
    for stycke in stycken:
        ord_i_stycke = len(stycke.split())
        if ord_raknare + ord_i_stycke > max_ord and aktuell:
            chunks.append('\n\n'.join(aktuell))
            aktuell, ord_raknare = [], 0
        aktuell.append(stycke)
        ord_raknare += ord_i_stycke
    if aktuell:
        chunks.append('\n\n'.join(aktuell))
    return [c for c in chunks if len(c.strip()) > 50]


def _chunka_och_indexera_dokument(dokument_id: int, fulltext: str, conn) -> int:
    """
    Chunkar och embeddar ett dokument och lagrar i gov_data.document_chunks.
    Hoppar över om chunks redan finns. Returnerar antal nya chunks.
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

    # Filtrera bort icke-svenska chunks (t.ex. parallelltext i int. överenskommelser)
    svenska_chunks = [c for c in chunks if _ar_svensk(c)]
    if not svenska_chunks:
        log.info(f"Inga svenska chunks kvar efter filtrering för dokument_id={dokument_id} — lagrar alla")
        svenska_chunks = chunks  # Fallback: behåll allt om inget är svenska

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
    log.info(f"Indexerade {len(svenska_chunks)}/{len(chunks)} svenska chunks for dokument_id={dokument_id}")
    return len(svenska_chunks)



# ---------------------------------------------------------------------------
# Verktyg — remissvar
# ---------------------------------------------------------------------------

@mcp.tool()
def gov_hamta_remissvar(
    remiss_url: str,
    batch_storlek: int = 5,
    fortsatt_fran_index: int = 0,
) -> dict:
    """
    Laddar ned och cachar remissvar för en remisspost i omgångar.

    Ska anropas efter explicit användarbekräftelse — nedladdningen kan ta
    flera minuter beroende på antal remissvar. Remissvar cachas lokalt i
    REMISSVAR_CACHE_TTL_DAYS dagar (standard 365).

    Använd batch_storlek och fortsatt_fran_index för att undvika timeout
    vid remisser med många remissvar (anropa upprepade gånger tills
    nasta_index är null).

    Args:
        remiss_url:          g0v.se-URL för remissposten.
        batch_storlek:       Antal remissvar att behandla per anrop (standard 5, max 50).
        fortsatt_fran_index: Startindex för nästa batch (hämtas från nasta_index i
                             föregående svar, 0 för första anropet).

    Returnerar: totalt antal remissvar, behandlat intervall, nasta_index (null = klart),
    lista med {remissinstans, status, antal_tecken}.
    """
    from pdf_lib import ladda_ned_pdf, extrahera_text, pdf_cache_sokvag
    import time as _time

    use_pg = _use_postgres()
    conn   = db.get_conn()
    cur    = conn.cursor()
    tabell = "gov_data.dokument" if use_pg else "dokument"
    plats  = "%s" if use_pg else "?"

    # Hämta remissposten
    cur.execute(
        f"SELECT id, typ_kod, bilagor FROM {tabell} WHERE url = {plats}",
        (remiss_url,)
    )
    rad = cur.fetchone()
    if not rad:
        cur.close(); conn.close()
        return {"fel": f"Remisspost hittades inte: {remiss_url}"}
    if rad[1] != "2099":
        cur.close(); conn.close()
        return {"fel": f"URL pekar inte på en remisspost (typ_kod={rad[1]})."}

    remiss_id  = rad[0]
    bilagor_raw = rad[2]
    bilagor    = json.loads(bilagor_raw) if isinstance(bilagor_raw, str) else (bilagor_raw or [])
    remissvar_bilagor = bilagor[1:]  # index 0 = remissmissiv, 1+ = remissvar

    if not remissvar_bilagor:
        cur.close(); conn.close()
        return {"info": "Inga remissvar hittades för denna remiss.", "antal": 0,
                "nasta_index": None, "remissvar": []}

    totalt_antal = len(remissvar_bilagor)

    # Beräkna vilket batch som ska behandlas
    batch_storlek   = max(1, min(batch_storlek, 50))
    start           = max(0, fortsatt_fran_index)
    slut            = min(start + batch_storlek, totalt_antal)
    detta_batch     = remissvar_bilagor[start:slut]
    nasta_index     = slut if slut < totalt_antal else None

    log.info(
        f"gov_hamta_remissvar: behandlar index {start}–{slut-1} av {totalt_antal} "
        f"(nasta_index={nasta_index})"
    )

    expires_at = datetime.now(timezone.utc) + timedelta(days=REMISSVAR_TTL_DAYS)
    modell     = _hamta_modell()
    rv_tabell  = "gov_data.remissvar" if use_pg else "remissvar"
    chunk_tab  = "gov_data.remissvar_chunks" if use_pg else None  # SQLite har ej chunktabell

    resultat = []
    for bilaga in detta_batch:
        instans = _extrahera_remissinstans(bilaga.get("name", ""))
        att_url = bilaga.get("url", "")
        if not att_url:
            resultat.append({"remissinstans": instans, "status": "HOPPAR (ingen URL)"})
            continue

        # Upsert i remissvar-tabellen
        if use_pg:
            cur.execute(f"""
                INSERT INTO {rv_tabell} (remiss_id, remissinstans, bilage_url, cache_utgar_vid)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (bilage_url) DO UPDATE SET
                    cache_utgar_vid = EXCLUDED.cache_utgar_vid,
                    remissinstans    = EXCLUDED.remissinstans
                RETURNING id, fulltext_md
            """, (remiss_id, instans, att_url, expires_at))
        else:
            cur.execute(f"""
                INSERT OR IGNORE INTO {rv_tabell}
                    (remiss_id, remissinstans, bilage_url, cache_utgar_vid)
                VALUES (?, ?, ?, ?)
            """, (remiss_id, instans, att_url, expires_at.isoformat()))
            cur.execute(f"SELECT id, fulltext_md FROM {rv_tabell} WHERE bilage_url = {plats}", (att_url,))
        conn.commit()
        rv_rad = cur.fetchone()
        if not rv_rad:
            cur.execute(f"SELECT id, fulltext_md FROM {rv_tabell} WHERE bilage_url = {plats}", (att_url,))
            rv_rad = cur.fetchone()
        rv_id, befintlig_text = rv_rad if rv_rad else (None, None)

        # Ladda ned och extrahera om fulltext saknas
        if befintlig_text:
            resultat.append({"remissinstans": instans, "status": "CACHE", "antal_tecken": len(befintlig_text)})
            continue

        sokvag = pdf_cache_sokvag(att_url)
        if not sokvag.exists():
            ok, fel = ladda_ned_pdf(att_url, sokvag)
            if not ok:
                resultat.append({
                    "remissinstans": instans,
                    "status": f"FEL (nedladdning: {fel[:120]})",
                })
                continue
            _time.sleep(0.5)

        # Verifiera att filen faktiskt finns på disk efter nedladdning
        if not sokvag.exists() or sokvag.stat().st_size == 0:
            resultat.append({
                "remissinstans": instans,
                "status": "FEL (fil saknas eller är tom efter nedladdning)",
            })
            continue

        text = extrahera_text(sokvag)
        if not text:
            log.info(f"Ingen text — försöker OCR-fallback: {sokvag.name}")
            from pdf_lib import ocr_pdf
            ocr_sokvag = ocr_pdf(sokvag)
            if ocr_sokvag:
                text = extrahera_text(ocr_sokvag)

        if not text:
            # Visa filstorlek så användaren kan se om det är en (möjligen korrupt) liten fil
            storlek_kb = sokvag.stat().st_size // 1024
            resultat.append({
                "remissinstans": instans,
                "status": f"FEL (ingen text efter OCR; pdf {storlek_kb} kB)",
            })
            continue

        # Spara fulltext
        if use_pg:
            cur.execute(f"""
                UPDATE {rv_tabell}
                SET fulltext_md = %s, fulltext_hamtad_vid = NOW(), pdf_sokvag = %s
                WHERE id = %s
            """, (text, str(sokvag), rv_id))
        else:
            cur.execute(f"""
                UPDATE {rv_tabell}
                SET fulltext_md = ?, fulltext_hamtad_vid = CURRENT_TIMESTAMP, pdf_sokvag = ?
                WHERE id = ?
            """, (text, str(sokvag), rv_id))
        conn.commit()

        # Chunka och embedda (endast PostgreSQL)
        if use_pg and rv_id:
            chunks = _chunka_text(text)
            cur.execute(f"DELETE FROM {chunk_tab} WHERE remissvar_id = %s", (rv_id,))
            for i, chunk in enumerate(chunks):
                vektor = modell.encode(chunk).tolist()
                cur.execute(f"""
                    INSERT INTO {chunk_tab}
                        (remissvar_id, chunk_index, chunk_text, remissinstans, remiss_url, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s::vector)
                """, (rv_id, i, chunk, instans, remiss_url, vektor))
            conn.commit()

        resultat.append({"remissinstans": instans, "status": "OK", "antal_tecken": len(text)})
        log.info(f"  Remissvar hämtat: {instans} ({len(text)} tecken)")

    cur.close()
    conn.close()
    ok = sum(1 for r in resultat if r.get("status") in ("OK", "CACHE"))
    return {
        "totalt_antal":  totalt_antal,
        "detta_batch":   len(resultat),
        "indexerade":    ok,
        "nasta_index":   nasta_index,
        "remissvar":     resultat,
    }


@mcp.tool()
def gov_list_remissinstanser(remiss_url: str) -> list[dict]:
    """
    Listar alla remissinstanser för en remisspost med cachestatus.

    Visar både vilka instanser som finns i remissmissivet och om deras
    remissvar är nedladdade och cachade lokalt.

    Args:
        remiss_url: g0v.se-URL för remissposten.

    Returnerar lista med: remissinstans, har_fulltext, cache_utgar_vid, bilage_url.
    """
    use_pg = _use_postgres()
    conn   = db.get_conn()
    cur    = conn.cursor()
    tabell = "gov_data.dokument" if use_pg else "dokument"
    rv_tab = "gov_data.remissvar" if use_pg else "remissvar"
    plats  = "%s" if use_pg else "?"

    # Hämta bilagor från remissposten
    cur.execute(f"SELECT id, bilagor FROM {tabell} WHERE url = {plats}", (remiss_url,))
    rad = cur.fetchone()
    if not rad:
        cur.close(); conn.close()
        return [{"fel": f"Remisspost hittades inte: {remiss_url}"}]

    remiss_id   = rad[0]
    bilagor_raw = rad[1]
    bilagor     = json.loads(bilagor_raw) if isinstance(bilagor_raw, str) else (bilagor_raw or [])
    remissvar_bilagor = bilagor[1:]

    # Hämta cachestatus
    cur.execute(
        f"SELECT bilage_url, remissinstans, fulltext_md, cache_utgar_vid FROM {rv_tab} WHERE remiss_id = {plats}",
        (remiss_id,)
    )
    cachad = {r[0]: r for r in cur.fetchall()}
    cur.close()
    conn.close()

    result = []
    for bilaga in remissvar_bilagor:
        instans = _extrahera_remissinstans(bilaga.get("name", ""))
        att_url = bilaga.get("url", "")
        rv = cachad.get(att_url)
        result.append({
            "remissinstans":    instans,
            "bilage_url":   att_url,
            "har_fulltext":     bool(rv and rv[2]),
            "cache_utgar_vid": str(rv[3]) if rv and rv[3] else None,
        })

    return result


@mcp.tool()
def gov_search_remissvar(
    remiss_url: str,
    query: str,
    remissinstans: str = "",
    top_k: int = 5,
) -> list[dict]:
    """
    Semantisk sökning i remissvar för en specifik remiss (kräver PostgreSQL).

    Söker i de cachade remissvarens chunks. Anropa gov_hamta_remissvar först
    för att säkerställa att remissvaren är nedladdade.

    Args:
        remiss_url:    g0v.se-URL för remissposten.
        query:         Sökfråga på svenska.
        remissinstans: Filtrera på specifik remissinstans (partiell matchning).
        top_k:         Antal relevanta stycken att returnera (standard: 5).

    Returnerar lista med: remissinstans, chunk_text, relevans.
    """
    if not _use_postgres():
        return [{"fel": "Semantisk sökning kräver PostgreSQL med pgvector."}]

    modell       = _hamta_modell()
    fraga_vektor = modell.encode(query).tolist()

    conn = db.get_conn()
    cur  = conn.cursor()

    if remissinstans:
        cur.execute("""
            SELECT rc.remissinstans, rc.chunk_text,
                   1 - (rc.embedding <=> %s::vector) AS relevans
            FROM gov_data.remissvar_chunks rc
            WHERE rc.remiss_url = %s
              AND rc.remissinstans ILIKE %s
            ORDER BY rc.embedding <=> %s::vector
            LIMIT %s
        """, (fraga_vektor, remiss_url, f"%{remissinstans}%", fraga_vektor, top_k))
    else:
        cur.execute("""
            SELECT rc.remissinstans, rc.chunk_text,
                   1 - (rc.embedding <=> %s::vector) AS relevans
            FROM gov_data.remissvar_chunks rc
            WHERE rc.remiss_url = %s
            ORDER BY rc.embedding <=> %s::vector
            LIMIT %s
        """, (fraga_vektor, remiss_url, fraga_vektor, top_k))

    result = [
        {
            "remissinstans": r[0],
            "chunk_text":    r[1],
            "relevans":      round(float(r[2]), 4),
        }
        for r in cur.fetchall()
    ]

    cur.close()
    conn.close()

    if not result:
        return [{"info": "Inga chunks hittades. Anropa gov_hamta_remissvar först."}]

    return result


# ---------------------------------------------------------------------------
# Uppstart
# ---------------------------------------------------------------------------

def _konfigurera_logging():
    """Konfigurerar logging beroende på transportläge.

    I stdio-läge skrivs MCP-protokollet till stdout och Claude Desktop
    läser stderr. Varje rad på stderr visas som en popup-varning — vid
    300+ remissvar blir det ohanterligt. Lösningen: dirigera all loggning
    till `logs/mcp_server.log` i stdio-läge så att stderr förblir tomt.
    I HTTP-läge är stderr legitim console-output (t.ex. för uvicorn).
    """
    log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

    if MCP_TRANSPORT == "http":
        # HTTP-läge: skriv till stderr som vanligt
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            force=True,
        )
        return None
    else:
        # stdio-läge: skriv till fil för att undvika popup-kaskaden i
        # Claude Desktop. Filen roteras inte automatiskt — skall hållas
        # i schack via separat logrotate eller manuell rensning.
        log_path = _SCRIPT_DIR / "logs" / "mcp_server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=str(log_path),
            filemode="a",
            level=log_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            force=True,
        )
        # Tysta stdlib-loggrar som annars kan skriva direkt till stderr
        for noisy in ("httpx", "urllib3", "requests"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        return log_path



# ---------------------------------------------------------------------------
# Verktyg — ärendeförteckningar (beslut pre-sept 2024)
# ---------------------------------------------------------------------------

@mcp.tool()
def gov_hamta_arendeforteckning(
    vecka_url: str,
    departement: str = "",
) -> dict:
    """
    Hämtar, cachar och indexerar ärendeförtecknings-PDF:er för en given vecka (pre-sept 2024).

    Anropas on-demand när användaren frågar om äldre regeringsbeslut. PDF:erna listas
    på sidan /arendeforteckningar/YYYY/MM/... och hämtas per departement.

    Args:
        vecka_url:   Relativ URL till veckosidan, t.ex.
                     /arendeforteckningar/2023/01/regeringens-arendeforteckningar-vecka-02-2023/
        departement: Filtrera på ett specifikt departement (partiell matchning, valfritt).
                     Om tomt hämtas alla departements PDF:er för veckan.

    Returnerar: antal nya PDF:er indexerade, antal chunks, lista med departement.
    """
    if not ARENDEFORTECKNING_AKTIV:
        return {"fel": "Ärendeförteckningar är inaktiverade (ARENDEFORTECKNING_AKTIV=false i .env)."}
    if not _use_postgres():
        return {"fel": "Ärendeförteckningar kräver PostgreSQL med pgvector."}

    import requests
    from bs4 import BeautifulSoup

    BASE_URL = "https://www.regeringen.se"
    full_url = BASE_URL + vecka_url if vecka_url.startswith("/") else vecka_url

    # Hämta veckosidan och extrahera PDF-länkar
    try:
        resp = requests.get(full_url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        return {"fel": f"Kunde inte hämta veckosidan: {e}"}

    soup = BeautifulSoup(resp.text, "lxml")
    pdf_lankar = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.endswith(".pdf") and "contentassets" in href:
            namn = a.get_text(strip=True)
            if departement and departement.lower() not in namn.lower():
                continue
            pdf_url = href if href.startswith("http") else BASE_URL + href
            pdf_lankar.append((namn, pdf_url))

    if not pdf_lankar:
        return {"info": "Inga PDF-länkar hittades på sidan.", "url": full_url}

    # Parsa vecka och år ur URL
    vecka_match = re.search(r"vecka[_-](\d+)[_-](\d{4})", vecka_url)
    vecka_nummer = int(vecka_match.group(1)) if vecka_match else None
    vecka_ar     = int(vecka_match.group(2)) if vecka_match else None

    conn = db.get_conn()
    cur  = conn.cursor()
    nya_pdf = 0
    totalt_chunks = 0
    dept_lista = []

    cache_dir = os.path.join(_SCRIPT_DIR, os.getenv("PDF_CACHE_DIR", "pdf_cache"))
    os.makedirs(cache_dir, exist_ok=True)

    for dept_namn, pdf_url in pdf_lankar:
        # Kolla om redan indexerad
        cur.execute(
            "SELECT id, fulltext_md FROM gov_data.arendeforteckning WHERE pdf_url = %s",
            (pdf_url,)
        )
        rad = cur.fetchone()

        if rad:
            af_id, fulltext = rad
            if fulltext:
                dept_lista.append(dept_namn)
                continue  # Redan indexerad

        # Ladda ned PDF
        pdf_filnamn = re.sub(r"[^a-z0-9_-]", "_", pdf_url.split("/")[-1].replace(".pdf", "")) + ".pdf"
        pdf_sokvag  = os.path.join(cache_dir, f"af_{vecka_ar}_{vecka_nummer}_{pdf_filnamn}")

        try:
            if not os.path.exists(pdf_sokvag):
                pdf_resp = requests.get(pdf_url, timeout=60)
                pdf_resp.raise_for_status()
                with open(pdf_sokvag, "wb") as f:
                    f.write(pdf_resp.content)
        except Exception as e:
            log.warning(f"Nedladdning misslyckades ({pdf_url}): {e}")
            continue

        # Extrahera text
        try:
            import pdf_lib as _pdf_lib
            fulltext = _pdf_lib.extrahera_text(pdf_sokvag)
        except Exception as e:
            log.warning(f"Textextrahering misslyckades ({pdf_sokvag}): {e}")
            fulltext = None

        if not fulltext or len(fulltext.strip()) < 50:
            continue

        # Spara i databasen
        if rad:
            af_id = rad[0]
            cur.execute(
                "UPDATE gov_data.arendeforteckning SET fulltext_md = %s, pdf_sokvag = %s WHERE id = %s",
                (fulltext, pdf_sokvag, af_id)
            )
        else:
            cur.execute("""
                INSERT INTO gov_data.arendeforteckning
                    (vecka_sida_url, vecka_nummer, vecka_ar, departement, pdf_url, pdf_sokvag, fulltext_md)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (vecka_url, vecka_nummer, vecka_ar, dept_namn, pdf_url, pdf_sokvag, fulltext))
            af_id = cur.fetchone()[0]

        conn.commit()

        # Chunka och indexera
        chunks = [c for c in _chunka_text(fulltext) if _ar_svensk(c)]
        if not chunks:
            chunks = _chunka_text(fulltext)

        modell = _hamta_modell()
        cur.execute(
            "DELETE FROM gov_data.arendeforteckning_chunks WHERE arendeforteckning_id = %s",
            (af_id,)
        )
        for i, chunk in enumerate(chunks):
            vektor = modell.encode(chunk).tolist()
            cur.execute("""
                INSERT INTO gov_data.arendeforteckning_chunks
                    (arendeforteckning_id, chunk_index, chunk_text, embedding)
                VALUES (%s, %s, %s, %s)
            """, (af_id, i, chunk, vektor))

        conn.commit()
        nya_pdf += 1
        totalt_chunks += len(chunks)
        dept_lista.append(dept_namn)
        log.info(f"Indexerade {len(chunks)} chunks för {dept_namn} vecka {vecka_nummer}/{vecka_ar}")

    cur.close()
    conn.close()

    return {
        "nya_pdf":        nya_pdf,
        "totalt_chunks":  totalt_chunks,
        "departement":    dept_lista,
        "vecka_nummer":   vecka_nummer,
        "vecka_ar":       vecka_ar,
    }


@mcp.tool()
def gov_search_arendeforteckning(
    query: str,
    from_date: str = "",
    to_date: str = "",
    departement: str = "",
    top_k: int = 5,
) -> list[dict]:
    """
    Semantisk sökning i indexerade ärendeförteckningar (beslut pre-sept 2024).

    Söker bland de PDF:er som hämtats via gov_hamta_arendeforteckning. Om inga
    resultat hittas för en period kan PDF:erna för den perioden behöva hämtas först.

    Args:
        query:       Sökfråga på svenska.
        from_date:   Startdatum YYYY-MM-DD (filtrerar på ISO-veckonyckel).
        to_date:     Slutdatum YYYY-MM-DD.
        departement: Filtrera på departement (partiell matchning, valfritt).
        top_k:       Antal resultat (standard: 5).

    Returnerar: lista med chunk_text, departement, vecka, relevans.
    """
    if not _use_postgres():
        return [{"fel": "Sökning kräver PostgreSQL med pgvector."}]

    modell = _hamta_modell()
    fraga_vektor = modell.encode(query).tolist()

    conn = db.get_conn()
    cur  = conn.cursor()

    villkor = ["1=1"]
    params  = [fraga_vektor]

    if from_date:
        nyckel = _datum_till_veckonyckel(from_date)
        if nyckel:
            villkor.append("(af.vecka_ar * 100 + af.vecka_nummer) >= %s")
            params.append(nyckel)
    if to_date:
        nyckel = _datum_till_veckonyckel(to_date)
        if nyckel:
            villkor.append("(af.vecka_ar * 100 + af.vecka_nummer) <= %s")
            params.append(nyckel)
    if departement:
        villkor.append("af.departement ILIKE %s")
        params.append(f"%{departement}%")

    params += [fraga_vektor, top_k]

    cur.execute(f"""
        SELECT afc.chunk_text,
               af.departement,
               af.vecka_nummer,
               af.vecka_ar,
               af.vecka_sida_url,
               1 - (afc.embedding <=> %s::vector) AS relevans
        FROM gov_data.arendeforteckning_chunks afc
        JOIN gov_data.arendeforteckning af ON af.id = afc.arendeforteckning_id
        WHERE {' AND '.join(villkor)}
        ORDER BY afc.embedding <=> %s::vector
        LIMIT %s
    """, params)

    result = [
        {
            "chunk_text":  r[0],
            "departement": r[1],
            "vecka":       f"vecka {r[2]}, {r[3]}",
            "vecka_url":   r[4],
            "relevans":    round(float(r[5]), 4),
        }
        for r in cur.fetchall()
    ]

    cur.close()
    conn.close()

    if not result:
        return [{"info": "Inga indexerade ärendeförteckningar matchade sökningen. Anropa gov_hamta_arendeforteckning för att hämta PDF:er för den aktuella perioden."}]

    return result


if __name__ == "__main__":
    _log_path = _konfigurera_logging()
    db.initiera_schema()

    if MCP_TRANSPORT == "http":
        # HTTP-läge med Bearer-token-autentisering
        import uvicorn
        from starlette.applications import Starlette
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import Response

        class BearerTokenMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if MCP_API_KEY:
                    auth = request.headers.get("Authorization", "")
                    if auth != f"Bearer {MCP_API_KEY}":
                        return Response("Obehörig", status_code=401)
                return await call_next(request)

        # Preladdning av embeddingmodell i HTTP-läge
        _hamta_modell()

        app = Starlette()
        app.add_middleware(BearerTokenMiddleware)
        mcp_app = mcp.get_asgi_app()
        app.mount("/", mcp_app)

        log.info(f"Startar HTTP-server på {MCP_HOST}:{MCP_PORT}")
        uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
    else:
        # stdio-läge (standard)
        log.info("Startar i stdio-läge")
        mcp.run()
