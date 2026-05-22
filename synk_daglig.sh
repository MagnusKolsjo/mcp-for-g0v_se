#!/usr/bin/env bash
# synk_daglig.sh — Daglig synkronisering av gov-data fran g0v.se och regeringen.se.
# Lasas av launchd (se.magnuskolsjo.mcp-gov-synk.plist) eller kan koras manuellt.
#
# Kortkommando (manuell körning):
#   bash synk_daglig.sh
#
# Schemalagd körning installeras via:
#   python 03_synka_data.py --installera-schema

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FIL="${SCRIPT_DIR}/.env"

if [ -f "${ENV_FIL}" ]; then
    set -a
    source "${ENV_FIL}"
    set +a
fi

PYTHON="${PYTHON_SOKVAG:-python3}"

echo "$(date '+%Y-%m-%d %H:%M:%S') Startar gov-synk"
"${PYTHON}" "${SCRIPT_DIR}/03_synka_data.py"
echo "$(date '+%Y-%m-%d %H:%M:%S') gov-synk klar"
