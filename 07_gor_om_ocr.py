"""
07_gor_om_ocr.py — Gör om extraktionen för dokument som OCR:ats med fel språk.

Äldre extraktioner OCR:ade skannade PDF:er med Tesseracts standardspråk
engelska. Svensk text fick då inga å, ä eller ö ("Lansstyrelsen i
S6dermanlands lan", "genomférande"). Skriptet hittar sådana dokument i
dokument-tabellen, laddar ned PDF:en (bilagor[0]) igen, extraherar den med
pdf_lib.extrahera (OCR-språk swe+eng+fra+deu) och ersätter fulltexten när
den nya texten är bättre. Chunks och embeddings byggs då om för dokumentet.

Urval: fulltext längre än 3 000 tecken, under 0,2 % av bokstäverna är
å/ä/ö/Å/Ä/Ö, och ordet " och " förekommer mer än 2 gånger per 1 000 tecken.
Remissvar ligger i en egen tabell och ingår inte.

Den nya texten räknas som bättre om den har fler å/ä/ö än den gamla och
är minst 80 % så lång. Varje behandlat dokument markeras i synkstatus
(nyckeln ocr_omkord:<id>), så en avbruten körning fortsätter där den
slutade. Dokument som gav fel markeras inte och försöks igen nästa gång.

Körning:
    .venv/bin/python3 07_gor_om_ocr.py --torrkorning   # lista urvalet
    .venv/bin/python3 07_gor_om_ocr.py --max 10        # de tio första
    .venv/bin/python3 07_gor_om_ocr.py --id 5638       # ett dokument, även om det är markerat
    .venv/bin/python3 07_gor_om_ocr.py                 # hela urvalet
"""

import argparse
import importlib
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).parent
load_dotenv(_SCRIPT_DIR / ".env")
sys.path.insert(0, str(_SCRIPT_DIR))

import db  # noqa: E402
import pdf_lib  # noqa: E402

log = logging.getLogger("gor_om_ocr")

MIN_LANGD        = 3000   # tecken fulltext
MAX_AAO_ANDEL    = 0.002  # andel av bokstäverna som är å/ä/ö
MIN_OCH_PER_1000 = 2.0    # förekomster av " och " per 1 000 tecken
MIN_LANGDKVOT    = 0.8    # ny text måste vara minst så här lång relativt den gamla
STATUSPREFIX     = "ocr_omkord:"

_AAO = set("åäöÅÄÖ")


# ---------------------------------------------------------------------------
# Mått på texten
# ---------------------------------------------------------------------------

def matt(text: str) -> dict:
    """Längd, antal bokstäver, antal å/ä/ö och antal ' och ' i en text."""
    return {
        "langd": len(text),
        "bokstaver": sum(1 for c in text if c.isalpha()),
        "aao": sum(1 for c in text if c in _AAO),
        "och": text.count(" och "),
    }


def ser_felocrad_ut(text: str) -> bool:
    """Svensk text utan svenska tecken — samma villkor som urvalet."""
    m = matt(text or "")
    if m["langd"] <= MIN_LANGD or m["bokstaver"] == 0:
        return False
    return (m["aao"] / m["bokstaver"] < MAX_AAO_ANDEL
            and m["och"] * 1000 / m["langd"] > MIN_OCH_PER_1000)


def ar_battre(ny: str, gammal: str) -> bool:
    """Fler å/ä/ö och inte väsentligt kortare."""
    return (matt(ny)["aao"] > matt(gammal)["aao"]
            and len(ny) >= MIN_LANGDKVOT * len(gammal))


def pdf_storlek_kb(bilage_namn: str) -> float | None:
    """Läser storleken ur bilagans namn, t.ex. '... (pdf 270 kB)' eller '(pdf 1 MB)'."""
    m = re.search(r"\(pdf\s+([\d.,]+)\s*(kB|MB|GB)\)", bilage_namn or "", re.IGNORECASE)
    if not m:
        return None
    varde = float(m.group(1).replace(",", "."))
    return varde * {"kb": 1, "mb": 1024, "gb": 1024 * 1024}[m.group(2).lower()]


# ---------------------------------------------------------------------------
# Urval
# ---------------------------------------------------------------------------

def _som_lista(bilagor) -> list:
    if isinstance(bilagor, str):
        try:
            return json.loads(bilagor)
        except ValueError:
            return []
    return bilagor or []


def hamta_urval(conn, dok_id: int | None = None) -> list[dict]:
    """Dokument som ser felOCR:ade ut och inte redan körts om.

    Med dok_id hämtas just det dokumentet, oavsett urval och markering.
    """
    tab = f"{db._prefix()}dokument"
    st  = f"{db._prefix()}synkstatus"
    ph  = db._ph()
    cur = conn.cursor()
    kolumner = "d.id, d.typ_kod, d.publicerad, d.titel, d.bilagor, d.fulltext_md"

    if dok_id is not None:
        cur.execute(f"SELECT {kolumner} FROM {tab} d WHERE d.id = {ph}", (dok_id,))
        rader = cur.fetchall()
    elif db._ar_postgres():
        # Villkoren räknas i databasen så att bara urvalet hämtas.
        cur.execute(f"""
            WITH d AS (
                SELECT {kolumner},
                       length(d.fulltext_md) AS langd,
                       length(regexp_replace(d.fulltext_md, '[^[:alpha:]]', '', 'g')) AS bokstaver,
                       length(regexp_replace(d.fulltext_md, '[^åäöÅÄÖ]', '', 'g')) AS aao,
                       (length(d.fulltext_md) - length(replace(d.fulltext_md, ' och ', ''))) / 5 AS och
                FROM {tab} d
                WHERE length(d.fulltext_md) > %s
                  AND NOT EXISTS (SELECT 1 FROM {st} s WHERE s.nyckel = %s::text || d.id)
            )
            SELECT id, typ_kod, publicerad, titel, bilagor, fulltext_md FROM d
            WHERE bokstaver > 0
              AND aao::float / bokstaver < %s
              AND och * 1000.0 / langd > %s
            ORDER BY id
        """, (MIN_LANGD, STATUSPREFIX, MAX_AAO_ANDEL, MIN_OCH_PER_1000))
        rader = cur.fetchall()
    else:
        cur.execute(f"""
            SELECT {kolumner} FROM {tab} d
            WHERE length(d.fulltext_md) > ?
              AND NOT EXISTS (SELECT 1 FROM {st} s WHERE s.nyckel = ? || d.id)
            ORDER BY d.id
        """, (MIN_LANGD, STATUSPREFIX))
        rader = [r for r in cur.fetchall() if ser_felocrad_ut(r[5])]
    cur.close()

    ut = []
    for r in rader:
        bilagor = _som_lista(r[4])
        forsta  = bilagor[0] if bilagor else {}
        ut.append({
            "id": r[0], "typ_kod": r[1], "ar": str(r[2])[:4] if r[2] else "",
            "titel": r[3] or "", "fulltext": r[5] or "",
            "bilage_url": forsta.get("url", ""),
            "bilage_namn": forsta.get("name", ""),
            "storlek_kb": pdf_storlek_kb(forsta.get("name", "")),
        })
    return ut


# ---------------------------------------------------------------------------
# Omkörning av ett dokument
# ---------------------------------------------------------------------------

def _markera(conn, dok_id: int, varde: dict) -> None:
    st = f"{db._prefix()}synkstatus"
    cur = conn.cursor()
    if db._ar_postgres():
        cur.execute(f"""
            INSERT INTO {st} (nyckel, varde, uppdaterad) VALUES (%s, %s, NOW())
            ON CONFLICT (nyckel) DO UPDATE SET varde = EXCLUDED.varde, uppdaterad = NOW()
        """, (f"{STATUSPREFIX}{dok_id}", json.dumps(varde, ensure_ascii=False)))
    else:
        cur.execute(f"""
            INSERT OR REPLACE INTO {st} (nyckel, varde, uppdaterad)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (f"{STATUSPREFIX}{dok_id}", json.dumps(varde, ensure_ascii=False)))
    conn.commit()
    cur.close()


def _ersatt_och_indexera(conn, dok_id: int, text: str) -> int:
    """Ersätter fulltexten och bygger om chunks och embeddings i en transaktion.

    Chunkningen och embeddingen görs av 04_indexera_chunks, samma kod som
    indexerar nya dokument, så att det omkörda dokumentet indexeras likadant.
    Misslyckas indexeringen rullas även textbytet tillbaka.
    """
    tab = f"{db._prefix()}dokument"
    cur = conn.cursor()
    try:
        if db._ar_postgres():
            cur.execute(f"UPDATE {tab} SET fulltext_md = %s, fulltext_hamtad_vid = NOW() WHERE id = %s",
                        (text, dok_id))
            cur.execute("DELETE FROM gov_data.document_chunks WHERE dokument_id = %s", (dok_id,))
            indexering = importlib.import_module("04_indexera_chunks")
            antal = indexering._indexera_dokument(dok_id, text, conn)  # gör commit
        else:
            # SQLite har ingen chunktabell.
            cur.execute(f"UPDATE {tab} SET fulltext_md = ?, fulltext_hamtad_vid = CURRENT_TIMESTAMP WHERE id = ?",
                        (text, dok_id))
            antal = 0
        conn.commit()
        return antal
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def behandla(doc: dict, conn) -> tuple[str, dict]:
    """Kör om ett dokument. Returnerar (utfall, detaljer), utfall ∈ förbättrad/oförändrad/fel."""
    if not doc["bilage_url"]:
        return "fel", {"orsak": "ingen bilaga"}

    sokvag = pdf_lib.pdf_cache_sokvag(doc["bilage_url"])
    try:
        if not sokvag.exists():
            ok, fel = pdf_lib.ladda_ned_pdf(doc["bilage_url"], sokvag)
            if not ok:
                return "fel", {"orsak": f"nedladdning: {fel[:200]}"}
        with open(sokvag, "rb") as f:
            if f.read(5) != b"%PDF-":
                return "fel", {"orsak": "filen är inte en PDF"}

        res = pdf_lib.extrahera(sokvag, kalla_id=f"dokument:{doc['id']}",
                                kalla_url=pdf_lib.fullstandig_url(doc["bilage_url"]))
        if res is None or len(res.text.strip()) <= 50:
            return "fel", {"orsak": "extraktionen gav ingen text"}

        fore, efter = matt(doc["fulltext"]), matt(res.text)
        detaljer = {
            "tid": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metod": res.metod, "sidor": res.sidor, "i_ocr_ko": res.i_ocr_ko,
            "langd_fore": fore["langd"], "langd_efter": efter["langd"],
            "aao_fore": fore["aao"], "aao_efter": efter["aao"],
            "fortfarande_dalig": ser_felocrad_ut(res.text),
        }
        if ar_battre(res.text, doc["fulltext"]):
            detaljer["chunks"] = _ersatt_och_indexera(conn, doc["id"], res.text)
            utfall = "förbättrad"
        else:
            utfall = "oförändrad"
        detaljer["resultat"] = utfall
        _markera(conn, doc["id"], detaljer)
        if detaljer["fortfarande_dalig"] and not res.i_ocr_ko:
            log.warning("id=%s ser fortfarande felOCR:ad ut men har textlager och "
                        "hamnade inte i OCR-kön", doc["id"])
        return utfall, detaljer
    except Exception as e:  # noqa: BLE001 – ett dokument får inte stoppa körningen
        log.exception("id=%s: oväntat fel", doc["id"])
        return "fel", {"orsak": f"{type(e).__name__}: {e}"}
    finally:
        try:
            sokvag.unlink(missing_ok=True)
        except OSError as e:
            log.warning("Kunde inte radera %s: %s", sokvag.name, e)


# ---------------------------------------------------------------------------
# Huvudprogram
# ---------------------------------------------------------------------------

def _visa_urval(urval: list[dict]) -> None:
    print(f"{'id':>6}  {'år':4}  {'typ':4}  {'pdf':>9}  titel  |  bilaga")
    for d in urval:
        storlek = f"{d['storlek_kb']:.0f} kB" if d["storlek_kb"] is not None else "?"
        print(f"{d['id']:>6}  {d['ar']:4}  {d['typ_kod']:4}  {storlek:>9}  "
              f"{d['titel'][:70]}  |  {pdf_lib.fullstandig_url(d['bilage_url'])}")
    kanda = [d["storlek_kb"] for d in urval if d["storlek_kb"] is not None]
    per_typ: dict[str, int] = {}
    for d in urval:
        per_typ[d["typ_kod"]] = per_typ.get(d["typ_kod"], 0) + 1
    print(f"\n{len(urval)} dokument ({', '.join(f'typ {t}: {n}' for t, n in sorted(per_typ.items()))}); "
          f"PDF:er sammanlagt {sum(kanda) / 1024:.1f} MB enligt bilagornas namn "
          f"({len(urval) - len(kanda)} utan storlek).")


def main() -> int:
    parser = argparse.ArgumentParser(description="Gör om extraktionen för felOCR:ade dokument")
    parser.add_argument("--torrkorning", action="store_true",
                        help="Lista urvalet utan att hämta eller ändra något")
    parser.add_argument("--max", type=int, default=None,
                        help="Behandla högst så här många dokument")
    parser.add_argument("--id", type=int, action="append", dest="idn",
                        help="Kör om just detta dokument (kan anges flera gånger); körs även om det redan är markerat")
    parser.add_argument("--paus", type=float, default=2.0,
                        help="Sekunders paus mellan nedladdningarna (standard 2)")
    args = parser.parse_args()

    conn = db._hamta_db()
    if args.idn:
        urval = [d for i in args.idn for d in hamta_urval(conn, i)]
    else:
        urval = hamta_urval(conn)
    if args.max is not None:
        urval = urval[:args.max]

    if args.torrkorning:
        _visa_urval(urval)
        conn.close()
        return 0

    log.info("%d dokument att köra om", len(urval))
    rakning = {"förbättrad": 0, "oförändrad": 0, "fel": 0}
    fortfarande_daliga, i_ko = [], 0
    start = time.monotonic()
    for nr, doc in enumerate(urval, 1):
        t0 = time.monotonic()
        utfall, detaljer = behandla(doc, conn)
        rakning[utfall] += 1
        i_ko += bool(detaljer.get("i_ocr_ko"))
        if detaljer.get("fortfarande_dalig"):
            fortfarande_daliga.append(doc["id"])
        if utfall == "fel":
            log.info("[%d/%d] id=%s FEL: %s", nr, len(urval), doc["id"], detaljer["orsak"])
        else:
            log.info("[%d/%d] id=%s %s (%s, %s sidor, %.0f s): å/ä/ö %d → %d, %d → %d tecken",
                     nr, len(urval), doc["id"], utfall.upper(), detaljer["metod"],
                     detaljer["sidor"], time.monotonic() - t0,
                     detaljer["aao_fore"], detaljer["aao_efter"],
                     detaljer["langd_fore"], detaljer["langd_efter"])
        if nr < len(urval):
            time.sleep(args.paus)
    conn.close()

    print("\n=== Sammanfattning ===")
    print(f"Förbättrade: {rakning['förbättrad']}")
    print(f"Oförändrade: {rakning['oförändrad']}")
    print(f"Fel:         {rakning['fel']}")
    print(f"I OCR-kön:   {i_ko}")
    if fortfarande_daliga:
        print(f"Ser fortfarande felOCR:ade ut: {len(fortfarande_daliga)} "
              f"(id {', '.join(map(str, fortfarande_daliga))})")
    print(f"Tid: {(time.monotonic() - start) / 60:.1f} min")
    return 1 if rakning["fel"] else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    for bullrig in ("httpx", "huggingface_hub", "sentence_transformers"):
        logging.getLogger(bullrig).setLevel(logging.WARNING)
    sys.exit(main())
