"""ASGI entrypoint: `uvicorn papertrail.web.asgi:app`.

`load_settings()` here (module import time) is the one place a module-level
call is acceptable — it's exactly what an ASGI server needs to find `app`,
mirrors how any other ASGI app is normally launched, and settings resolution
itself has no side effects (no directory creation, no client construction;
see papertrail.config's docstring). Everything CorpusRegistry/Store/routes
need is still built inside create_app's factory, not here.
"""
from .app import create_app

app = create_app()
