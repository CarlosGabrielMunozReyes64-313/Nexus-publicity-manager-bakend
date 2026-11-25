import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.id_token import verify_oauth2_token

from app.config import config
from app.db import Conn, auditar, insertar, transaccion, uno
from app.seguridad.permisos import ETIQUETAS, publicos

router = APIRouter(prefix="/api")
COOKIE = "nexus_sesion"


def digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def cifrar(value: str) -> bytes:
    return Fernet(config().clave_cifrado.get_secret_value().encode()).encrypt(value.encode())


def descifrar(value: bytes) -> str:
    return Fernet(config().clave_cifrado.get_secret_value().encode()).decrypt(value).decode()


@dataclass(frozen=True)
class Cuenta:
    """Persona con sesión válida: un administrador o un usuario aprobado."""

    id: str
    correo: str
    nombre: str
    rol: str

    @property
    def actor(self) -> str:
        # Valor de nexus.actor en la auditoría: los administradores conservan «admin».
        return "admin" if self.rol == "administrador" else "usuario"


SIN_PERMISO = "Su cuenta no tiene permiso para esta acción"


def cuenta_actual(request: Request) -> Cuenta:
    """Sesión válida o 401 (vencida, revocada, inexistente o cuenta desactivada)."""
    token = request.cookies.get(COOKIE, "")
    if not token:
        raise HTTPException(401, "Inicie sesión para continuar")
    with transaccion() as c:
        r = uno(
            c,
            "SELECT a.id,a.correo,a.nombre,a.rol FROM sesiones s JOIN administradores a ON a.id=s.administrador_id WHERE s.token_hash=%s AND s.revocada_en IS NULL AND s.expira_en>now() AND a.activo",
            (digest(token),),
            False,
        )
    if not r:
        raise HTTPException(401, "Su sesión venció o no es válida. Inicie sesión de nuevo")
    return Cuenta(str(r["id"]), str(r["correo"]), str(r["nombre"]), str(r["rol"]))


def administrador(request: Request) -> str:
    """Solo administradores: 401 sin sesión válida, 403 con sesión válida sin permiso."""
    cuenta = cuenta_actual(request)
    if cuenta.rol != "administrador":
        raise HTTPException(403, SIN_PERMISO)
    return cuenta.id


def sesion(cuenta_id: str) -> RedirectResponse:
    token = secrets.token_urlsafe(32)
    with transaccion() as c:
        r = uno(c, "SELECT rol FROM administradores WHERE id=%s AND activo", (cuenta_id,))
        actor = "admin" if r["rol"] == "administrador" else "usuario"
        for k, v in {
            "actor": actor,
            "administrador_id": cuenta_id,
            "accion": "inicio_sesion",
        }.items():
            c.execute("SELECT set_config(%s, %s, true)", ("nexus." + k, v))
        c.execute(
            "INSERT INTO sesiones(administrador_id,token_hash,expira_en) VALUES(%s,%s,now()+interval '12 hours')",
            (cuenta_id, digest(token)),
        )
        c.execute("UPDATE administradores SET ultimo_acceso=now() WHERE id=%s", (cuenta_id,))
        auditar(c, "inicio_sesion", "administradores", cuenta_id)
    response = RedirectResponse(config().app_url + "/", status_code=302)
    response.set_cookie(
        COOKIE,
        token,
        max_age=43200,
        httponly=True,
        secure=config().app_env == "production",
        samesite="lax",
        path="/",
    )
    return response


@router.get("/auth/session")
def comprobar(request: Request) -> dict[str, Any]:
    cuenta = cuenta_actual(request)
    return {
        "ok": True,
        "cuenta": {
            "id": cuenta.id,
            "correo": cuenta.correo,
            "nombre": cuenta.nombre,
            "rol": cuenta.rol,
            "rolEtiqueta": ETIQUETAS.get(cuenta.rol, cuenta.rol),
        },
        "permisos": publicos(cuenta.rol),
    }


@router.get("/auth/logout")
def logout(request: Request) -> RedirectResponse:
    token = request.cookies.get(COOKIE, "")
    with transaccion() as c:
        r = uno(
            c,
            "UPDATE sesiones s SET revocada_en=now() FROM administradores a WHERE a.id=s.administrador_id AND s.token_hash=%s AND s.revocada_en IS NULL RETURNING s.administrador_id, a.rol",
            (digest(token),),
            False,
        )
        if r:
            c.execute(
                "SELECT set_config('nexus.actor',%s,true), set_config('nexus.administrador_id',%s,true)",
                ("admin" if r["rol"] == "administrador" else "usuario", str(r["administrador_id"])),
            )
            auditar(c, "cierre_sesion", "administradores", str(r["administrador_id"]))
    response = RedirectResponse(config().app_url + "/login", status_code=302)
    response.delete_cookie(COOKIE, path="/")
    return response


def iniciar(finalidad: str, admin: str | None = None) -> RedirectResponse:
    cfg = config()
    if not cfg.google_client_id:
        raise HTTPException(503, "Configure las credenciales de Google")
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    with transaccion() as c:
        c.execute("DELETE FROM oauth_estados WHERE expira_en<now()")
        insertar(
            c,
            "oauth_estados",
            {
                "hash": digest(state),
                "nonce": nonce,
                "verifier": verifier,
                "finalidad": finalidad,
                "administrador_id": admin,
            },
        )
    callback = "/auth/google/callback" if finalidad == "login" else "/integraciones/gmail/callback"
    scopes = (
        "openid email profile"
        if finalidad == "login"
        else "openid email https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly"
    )
    params = {
        "client_id": cfg.google_client_id,
        "redirect_uri": cfg.app_url + "/api" + callback,
        "response_type": "code",
        "scope": scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": base64.urlsafe_b64encode(digest(verifier)).decode().rstrip("="),
        "code_challenge_method": "S256",
    }
    if finalidad == "gmail":
        params.update(access_type="offline", prompt="consent")
    response = RedirectResponse(
        "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params), status_code=302
    )
    response.set_cookie(
        "nexus_oauth",
        state,
        max_age=600,
        httponly=True,
        secure=cfg.app_env == "production",
        samesite="lax",
        path="/api",
    )
    return response


@router.get("/auth/google")
def google(request: Request) -> RedirectResponse:
    cfg = config()
    if cfg.app_env == "development" and cfg.auth_dev_bypass:
        # Solo desarrollo (la configuración lo prohíbe en producción): simula la identidad
        # verificada de otra persona para probar solicitudes sin Google.
        como = request.query_params.get("como", "").strip().lower()
        if como and "@" in como:
            return resolver_identidad(como, request.query_params.get("nombre", "")[:200], "")
        with transaccion() as c:
            r = uno(
                c, "SELECT id FROM administradores WHERE correo=%s AND activo", (cfg.admin_correo,)
            )
        return sesion(str(r["id"]))
    return iniciar("login")


@router.get("/integraciones/gmail/autorizar")
def gmail_autorizar(request: Request) -> RedirectResponse:
    return iniciar("gmail", administrador(request))


def intercambio(request: Request, finalidad: str) -> tuple[dict[str, Any], dict[str, Any]]:
    cfg = config()
    state = request.query_params.get("state", "")
    if not state or not hmac.compare_digest(state, request.cookies.get("nexus_oauth", "")):
        raise HTTPException(400, "Solicitud OAuth no válida")
    with transaccion() as c:
        row = uno(
            c,
            "DELETE FROM oauth_estados WHERE hash=%s AND finalidad=%s AND expira_en>now() RETURNING *",
            (digest(state), finalidad),
            False,
        )
    if not row:
        raise HTTPException(400, "Solicitud OAuth vencida")
    callback = "/auth/google/callback" if finalidad == "login" else "/integraciones/gmail/callback"
    with httpx.Client(timeout=20) as client:
        response = client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": request.query_params.get("code", ""),
                "client_id": cfg.google_client_id,
                "client_secret": cfg.google_client_secret.get_secret_value(),
                "code_verifier": row["verifier"],
                "grant_type": "authorization_code",
                "redirect_uri": cfg.app_url + "/api" + callback,
            },
        )
        response.raise_for_status()
        tokens = response.json()
    claims = verify_oauth2_token(tokens["id_token"], GoogleRequest(), cfg.google_client_id)  # type: ignore[no-untyped-call]
    correo = str(claims.get("email", "")).strip().lower()
    if claims.get("nonce") != row["nonce"] or not claims.get("email_verified") or not correo:
        raise HTTPException(403, "Cuenta no autorizada")
    tokens["correo"] = correo
    tokens["hd"] = str(claims.get("hd") or "").lower()
    tokens["nombre"] = " ".join(str(claims.get("name") or "").split())[:200]
    tokens["sub"] = str(claims.get("sub") or "")[:255]
    return tokens, row


def a_login(parametro: str) -> RedirectResponse:
    response = RedirectResponse(config().app_url + "/login?" + parametro, status_code=302)
    response.delete_cookie("nexus_oauth", path="/api")
    return response


def registrar_solicitud(c: Conn, correo: str, nombre: str, sub: str) -> tuple[str, bool]:
    """Una única solicitud por correo verificado. Devuelve (estado, es_nueva).

    El INSERT ... ON CONFLICT es atómico: dos identificaciones simultáneas de la misma
    persona crean una sola fila y solo la que la crea avisa a los administradores.
    """
    from app.notificaciones import notificar_administradores

    r = uno(
        c,
        """INSERT INTO solicitudes_acceso(correo,nombre,google_sub) VALUES(%s,%s,%s)
        ON CONFLICT ON CONSTRAINT solicitudes_acceso_correo_uq DO UPDATE SET
          ultima_identificacion_en=now(),
          identificaciones=solicitudes_acceso.identificaciones+1,
          nombre=coalesce(solicitudes_acceso.nombre, EXCLUDED.nombre),
          google_sub=coalesce(solicitudes_acceso.google_sub, EXCLUDED.google_sub)
        RETURNING id, estado, (xmax = 0) AS nueva""",
        (correo, nombre or None, sub or None),
    )
    if r["nueva"]:
        quien = f"{nombre} ({correo})" if nombre else correo
        notificar_administradores(
            c,
            "solicitud_acceso",
            "Nueva solicitud de acceso",
            f"{quien} pidió acceso a NEXUS con Google. Revísela para aprobarla o rechazarla.",
            f"/solicitudes-acceso?id={r['id']}",
            f"solicitud:{r['id']}",
        )
    return str(r["estado"]), bool(r["nueva"])


def resolver_identidad(correo: str, nombre: str, sub: str) -> RedirectResponse:
    """Decide qué ocurre con una identidad de Google ya verificada (correo verificado).

    - Cuenta existente activa (administrador o usuario aprobado): inicia sesión con su rol.
    - Cuenta existente desactivada: sin acceso. Nunca se reabre por el flujo de solicitudes.
    - Sin cuenta: crea o consulta su solicitud. No emite la cookie de sesión.
    """
    correo = correo.strip().lower()
    with transaccion("sistema", accion="identificacion_google") as c:
        cuenta = uno(c, "SELECT id, activo FROM administradores WHERE correo=%s", (correo,), False)
        if not cuenta:
            estado, nueva = registrar_solicitud(c, correo, nombre, sub)
    if cuenta:
        if not cuenta["activo"]:
            return a_login("error=acceso_denegado")
        response = sesion(str(cuenta["id"]))
        response.delete_cookie("nexus_oauth", path="/api")
        return response
    if estado == "rechazada":
        return a_login("solicitud=rechazada")
    # «aprobada» sin cuenta no ocurre: la aprobación crea la cuenta en la misma transacción.
    return a_login("solicitud=" + ("enviada" if nueva else "pendiente"))


@router.get("/auth/google/callback")
def callback(request: Request) -> RedirectResponse:
    if request.query_params.get("error"):
        return a_login("error=cancelado")
    try:
        tokens, _ = intercambio(request, "login")
    except (HTTPException, ValueError, KeyError, httpx.HTTPError):
        return a_login("error=acceso_denegado")
    return resolver_identidad(tokens["correo"], tokens.get("nombre", ""), tokens.get("sub", ""))


GMAIL_PERMISOS = {
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
}


def volver_a_configuracion(resultado: str) -> RedirectResponse:
    response = RedirectResponse(
        config().app_url + "/configuracion?gmail=" + resultado, status_code=302
    )
    response.delete_cookie("nexus_oauth", path="/api")
    return response


@router.get("/integraciones/gmail/callback")
def gmail_callback(request: Request) -> RedirectResponse:
    """Vuelve siempre a Configuración con un resultado legible, nunca con JSON crudo."""
    if request.query_params.get("error"):
        return volver_a_configuracion("cancelado")
    try:
        tokens, row = intercambio(request, "gmail")
    except (HTTPException, ValueError, KeyError, httpx.HTTPError):
        return volver_a_configuracion("solicitud")
    try:
        admin = administrador(request)
    except HTTPException:
        return volver_a_configuracion("sesion")
    cuenta = config().gmail_cuenta_envio.strip().lower()
    if admin != str(row["administrador_id"]) or not cuenta or tokens["correo"] != cuenta:
        return volver_a_configuracion("cuenta")
    if not tokens.get("refresh_token"):
        return volver_a_configuracion("sin_conexion")
    permisos = set(str(tokens.get("scope", "")).split())
    if not GMAIL_PERMISOS <= permisos:
        return volver_a_configuracion("permisos")
    with transaccion("admin", admin, "conectar_gmail") as c:
        c.execute(
            "UPDATE integraciones SET credenciales_cifradas=%s,cuenta=%s,permisos=%s,estado='conectada',detalle=%s WHERE id='gmail'",
            (
                cifrar(tokens["refresh_token"]),
                tokens["correo"],
                sorted(permisos),
                "Conectada con " + tokens["correo"],
            ),
        )
    return volver_a_configuracion("conectado")
