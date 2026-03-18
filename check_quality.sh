#!/usr/bin/env bash
set -euo pipefail

echo "[1/4] Running tests..."
python3 -m pytest -q tests

echo "[2/4] Running lint (ruff)..."
python3 -m ruff check .

echo "[3/4] Running type checks (mypy)..."
python3 -m mypy --ignore-missing-imports autobot.py tests

echo "[4/4] Running security scan (bandit)..."
python3 -m bandit -q -r autobot.py

echo "All checks passed."
