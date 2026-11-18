import base64
import hashlib
import hmac
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.id_token import verify_oauth2_token

from app.config import config
from app.db import auditar, insertar, transaccion, uno

router = APIRouter(prefix="/api")
COOKIE = "nexus_sesion"


def digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def cifrar(value: str) -> bytes:
    return Fernet(config().clave_cifrado.get_secret_value().encode()).encrypt(value.encode())


def descifrar(value: bytes) -> str:
    return Fernet(config().clave_cifrado.get_secret_value().encode()).decrypt(value).decode()


def administrador(request: Request) -> str:
    token = request.cookies.get(COOKIE, "")
    with transaccion() as c:
        r = uno(
            c,
            "SELECT a.id FROM sesiones s JOIN administradores a ON a.id=s.administrador_id WHERE s.token_hash=%s AND s.revocada_en IS NULL AND s.expira_en>now() AND a.activo",
            (digest(token),),
            False,
        )
    if not r:
        raise HTTPException(401, "Inicie sesión para continuar")
    return str(r["id"])


def sesion(admin: str) -> RedirectResponse:
    token = secrets.token_urlsafe(32)
    with transaccion("admin", admin, "inicio_sesion") as c:
        c.execute(
            "INSERT INTO sesiones(administrador_id,token_hash,expira_en) VALUES(%s,%s,now()+interval '12 hours')",
            (admin, digest(token)),
        )
        c.execute("UPDATE administradores SET ultimo_acceso=now() WHERE id=%s", (admin,))
        auditar(c, "inicio_sesion", "administradores", admin)
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
def comprobar(request: Request) -> dict[str, bool]:
    administrador(request)
    return {"ok": True}


@router.get("/auth/logout")
def logout(request: Request) -> RedirectResponse:
    token = request.cookies.get(COOKIE, "")
    with transaccion() as c:
        r = uno(
            c,
            "UPDATE sesiones SET revocada_en=now() WHERE token_hash=%s AND revocada_en IS NULL RETURNING administrador_id",
            (digest(token),),
            False,
        )
        if r:
            c.execute(
                "SELECT set_config('nexus.actor','admin',true), set_config('nexus.administrador_id',%s,true)",
                (str(r["administrador_id"]),),
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
def google() -> RedirectResponse:
    cfg = config()
    if cfg.app_env == "development" and cfg.auth_dev_bypass:
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
    if (
        claims.get("nonce") != row["nonce"]
        or not claims.get("email_verified")
        or str(claims.get("email", "")).strip().lower() != cfg.admin_correo.strip().lower()
    ):
        raise HTTPException(403, "Cuenta no autorizada")
    tokens["correo"] = claims["email"].lower()
    return tokens, row


@router.get("/auth/google/callback")
def callback(request: Request) -> RedirectResponse:
    try:
        tokens, _ = intercambio(request, "login")
        with transaccion() as c:
            r = uno(
                c, "SELECT id FROM administradores WHERE correo=%s AND activo", (tokens["correo"],)
            )
        response = sesion(str(r["id"]))
        response.delete_cookie("nexus_oauth", path="/api")
        return response
    except (HTTPException, ValueError, KeyError, httpx.HTTPError):
        return RedirectResponse(config().app_url + "/login?error=acceso_denegado", status_code=302)


@router.get("/integraciones/gmail/callback")
def gmail_callback(request: Request) -> RedirectResponse:
    tokens, row = intercambio(request, "gmail")
    admin = administrador(request)
    if (
        admin != str(row["administrador_id"])
        or tokens["correo"] != config().gmail_cuenta_envio.lower()
    ):
        raise HTTPException(403, "La cuenta no coincide con la cuenta de envío")
    if not tokens.get("refresh_token"):
        raise HTTPException(400, "Google no entregó permiso sin conexión; vuelva a autorizar")
    with transaccion("admin", admin, "conectar_gmail") as c:
        c.execute(
            "UPDATE integraciones SET credenciales_cifradas=%s,cuenta=%s,permisos=%s,estado='conectada',detalle=NULL WHERE id='gmail'",
            (cifrar(tokens["refresh_token"]), tokens["correo"], tokens.get("scope", "").split()),
        )
    response = RedirectResponse(config().app_url + "/configuracion", status_code=302)
    response.delete_cookie("nexus_oauth", path="/api")
    return response
