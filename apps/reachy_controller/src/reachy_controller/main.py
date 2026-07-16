"""Reachy Controller ASGI application and command-line entry point."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from reachy_controller.app_supervisor import AppSupervisor
from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.daemon_proxy import router as daemon_router
from reachy_controller.daemon_supervisor import DaemonSupervisor
from reachy_controller.routes.apps import router as apps_router
from reachy_controller.routes.audio import router as audio_router
from reachy_controller.routes.camera import router as camera_router
from reachy_controller.routes.controls import router as controls_router
from reachy_controller.routes.daemon import router as daemon_control_router
from reachy_controller.routes.expressions import ExpressionPlayer
from reachy_controller.routes.expressions import router as expressions_router
from reachy_controller.shared_streams import StateBroadcaster, StatsBroadcaster
from reachy_controller.routes.system_stats import router as system_stats_router
from reachy_controller.routes.telemetry import (
    root_router as telemetry_root_router,
)
from reachy_controller.routes.telemetry import (
    router as telemetry_router,
)

load_dotenv()

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"


class NoCacheMiddleware(BaseHTTPMiddleware):
    """Middleware to set Cache-Control: no-cache for static assets and index."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Response]
    ) -> Response:
        response = await call_next(request)
        # Set no-cache for root index and all static assets
        if request.url.path == "/" or request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(proxy: DaemonProxy | None = None) -> FastAPI:
    daemon_proxy = proxy or DaemonProxy(
        os.getenv("DAEMON_URL", "http://127.0.0.1:8000"),
        timeout=float(os.getenv("DAEMON_TIMEOUT", "2.0")),
    )
    app_supervisor = AppSupervisor(daemon_proxy=daemon_proxy)
    daemon_supervisor = DaemonSupervisor(daemon_proxy=daemon_proxy)
    state_broadcaster = StateBroadcaster(daemon_proxy)
    stats_broadcaster = StatsBroadcaster(daemon_proxy)
    expression_player = ExpressionPlayer(daemon_proxy)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await app.state.daemon_proxy.start()
        app.state.app_supervisor.reconcile_orphans()
        app.state.daemon_supervisor.reconcile_orphans()
        try:
            yield
        finally:
            await app.state.expression_player.aclose()
            await app.state.state_broadcaster.aclose()
            await app.state.stats_broadcaster.aclose()
            app.state.app_supervisor.stop_all()
            await app.state.daemon_proxy.close()

    app = FastAPI(title="Reachy Controller", version="0.1.0", lifespan=lifespan)
    app.state.daemon_proxy = daemon_proxy
    app.state.state_broadcaster = state_broadcaster
    app.state.stats_broadcaster = stats_broadcaster
    app.state.app_supervisor = app_supervisor
    app.state.daemon_supervisor = daemon_supervisor
    app.state.expression_player = expression_player
    app.add_middleware(NoCacheMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Map validation errors to 400 instead of FastAPI default 422."""
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid request body"},
        )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.include_router(telemetry_router)
    app.include_router(telemetry_root_router)
    app.include_router(apps_router)
    app.include_router(system_stats_router)
    app.include_router(controls_router)
    app.include_router(audio_router)
    app.include_router(camera_router)
    app.include_router(daemon_control_router)
    app.include_router(expressions_router)
    app.include_router(daemon_router)
    return app


app = create_app()


def main() -> None:
    uvicorn.run(
        "reachy_controller.main:app",
        host=os.getenv("CONTROLLER_HOST", "127.0.0.1"),
        port=int(os.getenv("CONTROLLER_PORT", "7861")),
        log_level=os.getenv("CONTROLLER_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
