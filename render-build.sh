#!/usr/bin/env bash
# Render / CI: use this as the full Build Command:
#   bash render-build.sh
#
set -euo pipefail
pip install --no-cache-dir -r requirements.txt
playwright install chromium
