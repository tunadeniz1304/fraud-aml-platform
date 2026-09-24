"""HTTP server entry point.

``uvicorn server:app`` (Docker) and ``python server.py`` (local) behave the
same: the pipeline is built by the FastAPI lifespan.
"""

from __future__ import annotations

import os

import uvicorn

from app.api.dashboard import app, create_app

__all__ = ["app", "create_app"]

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")  # noqa: S104 - konteyner içi bağlama
    uvicorn.run(app, host=host, port=int(os.getenv("PORT", "8000")))
