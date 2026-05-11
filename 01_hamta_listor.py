"""
01_hamta_listor.py — Hämtar JSON-listor från g0v.se och lagrar metadata i databasen.

Körbar ingångspunkt. Biblioteksfunktionerna finns i hamta_listor_lib.py.

Körning:
  python 01_hamta_listor.py
  python 01_hamta_listor.py --tvinga
"""
import argparse
from hamta_listor_lib import kor

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hämtar JSON-listor från g0v.se")
    parser.add_argument("--tvinga", action="store_true",
                        help="Hämta om listorna även om ingen ny data indikeras")
    args = parser.parse_args()
    kor(tvinga=args.tvinga)
