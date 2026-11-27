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


def activar_formulario_inicial(c: db.Conn) -> None:
    """Compatibilidad con instalaciones previas.

    Antes el receptor aceptaba envíos firmados aunque la integración figurara como
    desconectada. Ahora el estado manda; para no cortar la captación de una instalación
    que nunca tocó esa integración, se conecta una sola vez si hay secreto configurado.
    Una desconexión explícita del administrador siempre se respeta.
    """
    if not config().formulario_webhook_secreto.get_secret_value():
        return
    tocada = db.uno(
        c,
        """SELECT 1 AS si FROM registro_auditoria WHERE entidad='integraciones'
        AND entidad_id='formulario' AND accion<>'insert' LIMIT 1""",
        (),
        False,
    )
    if not tocada:
        c.execute(
            "UPDATE integraciones SET estado='conectada',detalle=%s WHERE id='formulario' AND estado<>'conectada'",
            ("Conectado: el receptor acepta envíos firmados del sitio",),
        )


def asegurar_administrador(c: db.Conn, correo: str) -> None:
    """El administrador configurado (ADMIN_CORREO) siempre existe con rol administrador.

    Si la fila no existe se crea activa. Si existe, solo se garantiza el rol: su estado
    activo o inactivo se respeta (una desactivación explícita no se deshace al arrancar).
    """
    c.execute(
        "INSERT INTO administradores(correo,nombre,rol) VALUES(%s,'Administrador NEXUS','administrador') "
        "ON CONFLICT(correo) DO UPDATE SET rol='administrador' WHERE administradores.rol<>'administrador'",
        (correo.strip().lower(),),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg = config()
    db.abrir(cfg.database_url.get_secret_value())
    with db.transaccion() as c:
        asegurar_administrador(c, cfg.admin_correo)
    with db.transaccion("sistema", accion="activar_formulario_inicial") as c:
        activar_formulario_inicial(c)
    yield
    db.cerrar()


def carpeta_frontend() -> Path:
    raiz = Path(__file__).resolve().parents[1]
    configurada = config().frontend_dist.strip()
    if not configurada:
        return (raiz / "frontend/dist").resolve()
    ruta = Path(configurada).expanduser()
    return (ruta if ruta.is_absolute() else raiz / ruta).resolve()


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
    from app.notificaciones import router as notificaciones_router
    from app.seguridad.accesos import router as accesos_router

    # Antes del enrutador general de /api, que atiende cualquier otra ruta.
    app.include_router(accesos_router)
    app.include_router(notificaciones_router)
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
        if ruta.startswith(("api/", "r/", "baja/", "img/")):
            raise HTTPException(404, "Ruta no encontrada")
        root = carpeta_frontend()
        target = (root / ruta).resolve()
        if not target.is_relative_to(root):
            raise HTTPException(404, "Ruta no encontrada")
        if target.is_file():
            return FileResponse(target)
        if (root / "index.html").exists():
            # index.html no se guarda en caché para que cada despliegue cargue sus assets nuevos.
            return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})
        raise HTTPException(
            503,
            "No se encontró el frontend compilado. Ejecute «npm run build» en la carpeta del "
            "frontend y defina FRONTEND_DIST con la ruta a su carpeta dist.",
        )

    return app
