"""
FastAPI application factory.

Start the server with::

    uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

Environment
-----------
Set ``OPENROUTER_API_KEY`` in a ``.env`` file or as an environment variable
before starting the server.  See ``.env.example`` for a template.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api.books import router as books_router
from backend.api.recordings import router as recordings_router
from backend.api.websocket_audio import router as ws_router
from backend.core.config import settings
from backend.core.database import create_all_tables

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Startup / shutdown lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Runs once at startup (before the first request) and once at shutdown.

    Startup
    -------
    * Ensure runtime audio directories exist.
    * Create all SQLAlchemy-managed tables (idempotent).

    Shutdown
    --------
    * Log a goodbye message (add cleanup as needed).
    """
    # Ensure data directories exist.
    Path("data").mkdir(exist_ok=True)
    Path(settings.audio.storage_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.audio.raw_storage_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.audio.vad_storage_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.audio.decoded_storage_dir).mkdir(parents=True, exist_ok=True)
    logger.info(
        "Audio directories ready: legacy=%s raw=%s vad=%s decoded=%s",
        settings.audio.storage_dir,
        settings.audio.raw_storage_dir,
        settings.audio.vad_storage_dir,
        settings.audio.decoded_storage_dir,
    )

    # Bootstrap the database.
    create_all_tables()
    logger.info("Application startup complete — %s", settings.app_name)

    yield  # ← application runs here

    logger.info("Application shutdown.")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Construct and configure the FastAPI application."""

    app = FastAPI(
        title=settings.app_name,
        description=(
            "Record verbal book summaries, transcribe via OpenRouter, "
            "detect chapter boundaries, and persist structured results."
        ),
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
        debug=settings.debug,
    )

    # ------------------------------------------------------------------ #
    # CORS — allow the frontend origin in development.
    # In production, restrict to your actual domain.
    # ------------------------------------------------------------------ #
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://localhost:5173",
            "http://127.0.0.1:8000",
            "http://localhost:8000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------ #
    # Routers
    # ------------------------------------------------------------------ #
    app.include_router(books_router)
    app.include_router(recordings_router)
    app.include_router(ws_router)

    # ------------------------------------------------------------------ #
    # Health check
    # ------------------------------------------------------------------ #
    @app.get("/api/health", tags=["meta"], summary="Health check")
    def health() -> dict:
        return {"status": "ok", "app": settings.app_name}

    # ------------------------------------------------------------------ #
    # Static files — serve WAV recordings so the browser can play them.
    # Mounted before the catch-all frontend mount so the path is resolved
    # correctly.  Files live at ./data/audio/{recording_id}.wav on the host
    # (and /app/data/audio/ inside Docker).
    # ------------------------------------------------------------------ #
    audio_dir = Path(settings.audio.vad_storage_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/media/audio",
        StaticFiles(directory=str(audio_dir)),
        name="audio_files",
    )
    logger.info("Serving VAD audio files from %s at /media/audio", audio_dir.resolve())

    # ------------------------------------------------------------------ #
    # Static files — serve the vanilla-JS frontend from ./frontend/
    # Mount last so it doesn't shadow API routes.
    # ------------------------------------------------------------------ #
    frontend_dir = Path("frontend")
    if frontend_dir.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=str(frontend_dir), html=True),
            name="frontend",
        )
        logger.info("Serving frontend static files from %s", frontend_dir.resolve())
    else:
        logger.info(
            "No 'frontend/' directory found — skipping static file mount. "
            "API is available at /api/."
        )

    return app


# ---------------------------------------------------------------------------
# Module-level app instance (used by uvicorn / gunicorn)
# ---------------------------------------------------------------------------

app = create_app()
