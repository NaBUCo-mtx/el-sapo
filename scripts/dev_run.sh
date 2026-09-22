#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m cyberdeck_pi.main --config config/dev.toml
