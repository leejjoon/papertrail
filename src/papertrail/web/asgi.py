"""ASGI entrypoint: `uvicorn papertrail.web.asgi:app`.

`load_settings()` here (module import time) is the one place a module-level
call is acceptable — it's exactly what an ASGI server needs to find `app`,
mirrors how any other ASGI app is normally launched, and settings resolution
itself has no side effects (no directory creation, no client construction;
see papertrail.config's docstring). Everything CorpusRegistry/Store/routes
need is still built inside create_app's factory, not here.

ROOT_PATH (optional): set this if the app is deployed behind a reverse
proxy under a path prefix (e.g. "/kb") — see create_app's docstring for
why this has to be threaded through explicitly rather than relying on
uvicorn's own --root-path alone (that fixes routing/OpenAPI, not the
literal <img src=...> URLs this app bakes into rendered HTML).
"""
import os

from .app import create_app

app = create_app(root_path=os.getenv("ROOT_PATH", ""))
