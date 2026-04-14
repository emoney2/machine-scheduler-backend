#!/usr/bin/env bash
# Render / CI: use this as the full Build Command:
#   bash render-build.sh
#
# Do NOT chain "playwright install" here unless you also install Playwright
# (see requirements-playwright.txt). The default image has no browser.
set -euo pipefail
pip install --no-cache-dir -r requirements.txt
