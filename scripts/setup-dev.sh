#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(dirname -- "$SCRIPT_DIR")
ENVIRONMENT=${1:-"$PROJECT_ROOT/venv-acceptance"}
BOOTSTRAP_PYTHON=${PYTHON:-python3}

"$BOOTSTRAP_PYTHON" "$SCRIPT_DIR/editable_install_guard.py" \
    --validate-environment "$ENVIRONMENT"
"$BOOTSTRAP_PYTHON" -m venv "$ENVIRONMENT"
"$ENVIRONMENT/bin/python" -m pip install --upgrade pip
"$ENVIRONMENT/bin/python" -m pip install -e "${PROJECT_ROOT}[dev]"
"$ENVIRONMENT/bin/python" "$SCRIPT_DIR/editable_install_guard.py"
