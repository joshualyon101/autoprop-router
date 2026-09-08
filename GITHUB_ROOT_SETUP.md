# GitHub root-layout package

This package is intentionally flat for GitHub web upload and Railway.

At repository root you should see `main.py`, `settings.py`, `requirements.txt`, `railway.toml`, etc.
There is no `app/` directory in this variant.

Railway start command:
`python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}`
