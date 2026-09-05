# AutoProp Router v0.1 — Railway Flat Layout

This version is intentionally flat for GitHub web upload.

Railway Root Directory: LEAVE BLANK.

Persistent Volume mount path:
/data

Railway variable:
SQLITE_PATH=/data/autoprop_router.sqlite3

Keep:
AUTOPROP_EXECUTION_MODE=shadow

The Dockerfile starts:
uvicorn main:app
