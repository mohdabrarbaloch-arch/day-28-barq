"""Barq - smart grid dispatch and monitoring platform.

Application factory. Everything the app needs is wired here so tests can build
an isolated instance against a temporary database.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth as auth_router
from app.api import grid as grid_router
from app.core.config import settings
from app.core.database import init_db
from app.core.middleware import RateLimitMiddleware, SecurityHeadersMiddleware

logger = logging.getLogger("barq")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_for_startup()
    init_db()
    logger.info(
        "Barq started: env=%s db=%s",
        settings.app_env,
        settings.database_url.split("://", 1)[0],
    )
    yield
    logger.info("Barq shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Barq",
        description=(
            "Smart grid dispatch and monitoring. Merit-order dispatch with ramp "
            "limits, minimum stable load, storage round-trip losses and N-1 "
            "reserve, where every setpoint carries the reason it was chosen."
        ),
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.include_router(auth_router.router)
    app.include_router(grid_router.router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/health", tags=["ops"], summary="Liveness probe")
    def health() -> dict:
        return {"status": "ok", "service": "barq", "version": "1.0.0"}

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def index():
        index_file = STATIC_DIR / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return JSONResponse(
            {
                "service": "Barq",
                "docs": "/docs",
                "note": "Dashboard assets are not present in this build.",
            }
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Return 422 with a readable list rather than FastAPI's nested dump."""
        problems = [
            {
                "field": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
                "message": err.get("msg", "invalid value"),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "Validation failed", "errors": problems},
        )

    return app


app = create_app()
