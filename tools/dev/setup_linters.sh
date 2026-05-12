#!/usr/bin/env bash
set -euo pipefail
echo "=== Installing dev tools ==="
pip install --user ruff==0.6.9 mypy==1.11 pre-commit==4.0.1 detect-secrets==1.5.0
echo "=== Installing pre-commit hooks ==="
pre-commit install --install-hooks
echo "=== Running initial scan ==="
detect-secrets scan > .secrets.baseline 2>/dev/null || echo "(no secrets found)"
echo "=== Initial ruff pass ==="
ruff check --fix core/tools/ || true
ruff format core/tools/ || true
echo "Done. Run 'pre-commit run --all-files' to verify."
