#!/usr/bin/env bash
# Copy the aiofilen library into the integration (HA loads it from there).
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf custom_components/filen/aiofilen
mkdir -p custom_components/filen/aiofilen
cp aiofilen/src/aiofilen/*.py aiofilen/src/aiofilen/py.typed custom_components/filen/aiofilen/
echo "Synced aiofilen into custom_components/filen/aiofilen"