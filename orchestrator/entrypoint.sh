#!/usr/bin/env bash
set -euo pipefail
python cli.py migrate
exec uvicorn main:app --host 0.0.0.0 --port 8000
