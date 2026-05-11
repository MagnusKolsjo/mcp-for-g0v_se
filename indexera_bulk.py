#!/usr/bin/env python3
"""
Bulk-indexering av dokument i gov_data.document_chunks.

Kör gov_indexera_bulk i omgångar tills alla dokument med fulltext är indexerade.
Startar om från början om inget index anges, eller fortsätter från --fran <index>.

Användning:
    python3 indexera_bulk.py
    python3 indexera_bulk.py --batch 15
    python3 indexera_bulk.py --fran 120
"""
import argparse
import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Importera mcp_server-funktionerna direkt (kräver samma venv)
import db
from mcp_server import gov_indexera_bulk


def main():
    parser = argparse.ArgumentParser(description="Bulk-indexera gov_data.dokument")
    parser.add_argument("--batch", type=int, default=10,
                        help="Antal dokument per omgång (standard 10, max 25)")
    parser.add_argument("--fran", type=int, default=0,
                        help="Starta från detta index (standard 0)")
    parser.add_argument("--paus", type=float, default=1.0,
                        help="Sekunders paus mellan omgångar (standard 1)")
    args = parser.parse_args()

    index        = args.fran
    batch        = min(args.batch, 25)
    totalt_chunks = 0
    omgang       = 0

    log.info(f"Startar bulk-indexering — batch={batch}, fran={index}")

    while True:
        omgang += 1
        svar = gov_indexera_bulk(batch_storlek=batch, fortsatt_fran_index=index)

        if "fel" in svar:
            log.error(f"Fel: {svar['fel']}")
            sys.exit(1)

        kvar   = svar["totalt_kvar"]
        antal  = svar["detta_batch"]
        chunks = svar["indexerade_chunks"]
        nasta  = svar["nasta_index"]

        totalt_chunks += chunks
        log.info(
            f"Omgång {omgang}: {antal} dok, {chunks} chunks — "
            f"kvar={kvar}, nasta_index={nasta}, totalt_chunks={totalt_chunks}"
        )

        if nasta is None:
            break

        index = nasta
        time.sleep(args.paus)

    log.info(f"=== Klart — {totalt_chunks} chunks indexerade totalt ===")


if __name__ == "__main__":
    main()
