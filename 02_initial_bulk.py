"""
02_initial_bulk.py — Engångsnedladdning av PDF:er för prioriterade dokumenttyper.

Körbar ingångspunkt. Biblioteksfunktionerna finns i pdf_lib.py.

Körning:
  python 02_initial_bulk.py
  python 02_initial_bulk.py --typ forordningsmotiv
  python 02_initial_bulk.py --om-indexera-alla
"""
import argparse
import logging
from pdf_lib import kor, BULK_TYPER

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk-nedladdning av PDF:er")
    parser.add_argument(
        "--typ",
        choices=["forordningsmotiv", "remissmissiv", "internationella"],
        help="Kör bara en dokumenttyp",
    )
    parser.add_argument(
        "--om-indexera-alla",
        action="store_true",
        help="Ladda ned och indexera om även dokument med befintlig fulltext",
    )
    args = parser.parse_args()

    typ_mappning = {
        "forordningsmotiv":  {"1326"},
        "remissmissiv":      {"2099"},
        "internationella":   {"1332"},
    }
    valda = typ_mappning.get(args.typ) if args.typ else None
    hoppa = not args.om_indexera_alla
    kor(typer=valda, hoppa_existerande=hoppa)
