from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db
from app.config import config
from app.seguridad.auth import router as auth_router

structlog.configure(
    processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()]
)
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg = config()
    db.abrir(cfg.database_url.get_secret_value())
    with db.transaccion() as c:
        c.execute(
            "INSERT INTO administradores(correo,nombre) VALUES(%s,'Administrador NEXUS') ON CONFLICT(correo) DO NOTHING",
            (cfg.admin_correo,),
        )
    yield
    db.cerrar()


def crear_app() -> FastAPI:
    cfg = config()
    app = FastAPI(
        title="NEXUS",
        lifespan=lifespan,
        docs_url="/api/docs" if cfg.app_env == "development" else None,
        openapi_url="/api/openapi.json" if cfg.app_env == "development" else None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def proteger(request: Request, call_next: object) -> Response:
        from collections.abc import Awaitable, Callable
        from typing import cast

        if (
            request.url.path.startswith("/api/")
            and request.method in ("POST", "PUT", "PATCH", "DELETE")
            and request.url.path != "/api/webhooks/formulario"
        ):
            if (
                request.headers.get("origin") != cfg.app_url
                or request.headers.get("content-type", "").split(";")[0].strip()
                != "application/json"
            ):
                return JSONResponse(
                    {"message": "Origen o formato de solicitud no permitido"}, status_code=403
                )
        try:
            response = await cast(Callable[[Request], Awaitable[Response]], call_next)(request)
        except Exception:
            log.error("solicitud_fallida", metodo=request.method)
            return JSONResponse(
                {"message": "No fue posible completar la solicitud"}, status_code=500
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse({"message": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"message": "Datos no válidos; revise los campos enviados"}, status_code=400
        )

    @app.exception_handler(psycopg.Error)
    async def postgres_error(request: Request, exc: psycopg.Error) -> JSONResponse:
        status, message = 500, "No fue posible completar la operación"
        if isinstance(exc, psycopg.errors.UniqueViolation):
            status, message = (
                409,
                db.UNIQUE.get(exc.diag.constraint_name or "", "El registro ya existe"),
            )
        elif isinstance(exc, psycopg.errors.ForeignKeyViolation):
            status = 400
            message = "El registro relacionado no existe o todavía está en uso"
        elif (
            isinstance(exc, (psycopg.errors.CheckViolation, psycopg.errors.RaiseException))
            and not exc.diag.constraint_name
        ):
            status = 400
            message = exc.diag.message_primary or "Los datos no cumplen las reglas del registro"
        elif isinstance(
            exc,
            (
                psycopg.errors.CheckViolation,
                psycopg.errors.NotNullViolation,
                psycopg.errors.InvalidTextRepresentation,
                psycopg.errors.NumericValueOutOfRange,
                psycopg.errors.InvalidDatetimeFormat,
            ),
        ):
            status, message = 400, "Los datos no cumplen las reglas del registro"
        elif isinstance(exc, psycopg.OperationalError):
            status, message = 503, "La base de datos no está disponible"
        return JSONResponse({"message": message}, status_code=status)

    app.include_router(auth_router)
    from app.publico.rutas import router as publico_router

    app.include_router(publico_router)
    # Los módulos de negocio se incorporan tras validar la base.
    try:
        from app.api.rutas import router

        app.include_router(router)
    except ModuleNotFoundError as exc:
        if exc.name != "app.api.rutas":
            raise

    @app.get("/health")
    def health() -> dict[str, str]:
        with db.transaccion() as c:
            c.execute("SELECT 1")
        return {"estado": "ok"}

    @app.get("/{ruta:path}")
    def spa(ruta: str) -> FileResponse:
        if ruta.startswith(("api/", "r/", "baja/")):
            raise HTTPException(404, "Ruta no encontrada")
        root = (Path(__file__).resolve().parents[1] / "frontend/dist").resolve()
        target = (root / ruta).resolve()
        if not target.is_relative_to(root):
            raise HTTPException(404, "Ruta no encontrada")
        if target.is_file():
            return FileResponse(target)
        if (root / "index.html").exists():
            return FileResponse(root / "index.html")
        raise HTTPException(503, "Compile el frontend antes de iniciar la aplicación")

    return app
