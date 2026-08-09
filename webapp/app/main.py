"""Compatibility shim — migration Step 4.

The FastAPI app (main.py's routes, previously constructed at import time as
a module-level `app = FastAPI(...)`) moved to src/papertrail/web/app.py as
`create_app(settings)`, plus web/asgi.py for the `app = create_app()`
module-level instance an ASGI server actually needs. This shim exists only
so the CLAUDE.md-documented run command
(`uvicorn webapp.app.main:app --port 8009 --host 0.0.0.0`) keeps working
unchanged during the migration; `papertrail.web.asgi:app` is the real,
supported entrypoint going forward (docs updated in migration Step 11).
"""
from papertrail.web.asgi import app  # noqa: F401
