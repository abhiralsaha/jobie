#!/usr/bin/env bash
# Runs the whole suite (works with plain unittest; pytest also works if installed).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests_ai -t . -v
