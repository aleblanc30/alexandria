"""FastAPI application entry point."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from pka.api.routers import (
    clusters,
    documents,
    images,
    ingestion,
    reading_lists,
    runs,
    search,
    settings,
    tag_training,
    tags,
    trends,
)
from pka.cli._logging import setup_logging
from pka.clustering.run_progress import reconcile_interrupted_runs
from pka.db.queries import init_db

# Configure logging as soon as the app is imported. uvicorn configures only its
# own loggers and leaves the root handler-less, so every ``pka.*`` INFO log —
# and the tracebacks from background ingestion jobs — would otherwise be dropped
# by Python's last-resort handler (WARNING+ only). Running this at import means
# it also applies inside each ``--reload`` worker. uvicorn's own loggers have
# ``propagate=False``, so this doesn't double-print their lines.
setup_logging()

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Alexandria API starting — initialising database…")
    init_db()
    # No clustering thread can outlive the process that owned it, so any run
    # still marked "running" here was interrupted and would otherwise wedge
    # both deletion and every new run.
    reconcile_interrupted_runs()
    yield
    log.info("Alexandria API shutting down.")


app = FastAPI(
    title="Alexandria",
    version="0.0.8",
    description="Local-first research library API",
    lifespan=lifespan,
)

# CORS only needed in dev — in production the frontend is served same-origin.
# Start the backend with ALEXANDRIA_DEV=1 to enable.
if os.environ.get("ALEXANDRIA_DEV") == "1":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

for router in (
    search,
    documents,
    images,
    clusters,
    runs,
    tags,
    trends,
    ingestion,
    reading_lists,
    tag_training,
    settings,
):
    app.include_router(router.router)

# Serve built frontend from dist/ in production
try:
    app.mount(
        "/",
        StaticFiles(directory="frontend/dist", html=True),
        name="static",
    )
except RuntimeError:
    # dist/ not built yet — dev mode, frontend served by Vite
    pass
