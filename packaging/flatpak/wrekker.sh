#!/bin/bash
set -euo pipefail
export WREKKER_MODELS_PATH="${XDG_DATA_HOME:-$HOME/.local/share}/wrekker/models"
exec python3 -m wrekker.ui.app "$@"
