import base64
import hashlib
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol

from app.config import config
from app.db import Conn, Row
from app.integraciones.conexiones import gmail


def message_id(clave: str) -> str:
    dominio = config().gmail_cuenta_envio.split("@")[-1]
    return "<" + hashlib.sha256(clave.encode()).hexdigest() + "@" + dominio + ">"


def mime(m: Row, marca: Row, baja: str, previo: str | None = None) -> EmailMessage:
    e = EmailMessage()
    e["From"] = formataddr((marca["nombre"], config().gmail_cuenta_envio))
    e["To"] = m["destinatario"]
    e["Subject"] = m.get("asunto") or "Re: Información NEXUS"
    e["Message-ID"] = message_id(m["clave_idempotencia"])
    e["X-Nexus-Idempotencia"] = m["clave_idempotencia"]
    e["List-Unsubscribe"] = "<" + baja + ">"
    e["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if previo:
        e["In-Reply-To"] = previo
        e["References"] = previo
    e.set_content(m["cuerpo"], charset="utf-8")
    return e


class Gmail(Protocol):
    def enviar(self, mensaje: EmailMessage, hilo: str | None) -> dict[str, str]: ...
    def buscar(self, clave: str) -> dict[str, str] | None: ...
    def hilo(self, ident: str) -> list[Row]: ...


class GmailReal:
    def __init__(self, c: Conn) -> None:
        self.api = gmail(c)

    def enviar(self, mensaje: EmailMessage, hilo: str | None) -> dict[str, str]:
        body = {"raw": base64.urlsafe_b64encode(mensaje.as_bytes()).decode()}
        if hilo:
            body["threadId"] = hilo
        r: dict[str, str] = self.api.users().messages().send(userId="me", body=body).execute()
        return r

    def buscar(self, clave: str) -> dict[str, str] | None:
        result = (
            self.api.users()
            .messages()
            .list(userId="me", q="rfc822msgid:" + message_id(clave))
            .execute()
        )
        if result.get("messages"):
            r: dict[str, str] = result["messages"][0]
            return r
        token = None
        while True:
            result = (
                self.api.users()
                .messages()
                .list(userId="me", q="in:sent newer_than:1d", maxResults=500, pageToken=token)
                .execute()
            )
            for x in result.get("messages", []):
                m = (
                    self.api.users()
                    .messages()
                    .get(
                        userId="me",
                        id=x["id"],
                        format="metadata",
                        metadataHeaders=["X-Nexus-Idempotencia"],
                    )
                    .execute()
                )
                if any(
                    h["name"].lower() == "x-nexus-idempotencia" and h["value"] == clave
                    for h in m["payload"]["headers"]
                ):
                    return {"id": m["id"], "threadId": m["threadId"]}
            token = result.get("nextPageToken")
            if not token:
                return None

    def hilo(self, ident: str) -> list[Row]:
        result: list[Row] = (
            self.api.users()
            .threads()
            .get(userId="me", id=ident, format="full")
            .execute()
            .get("messages", [])
        )
        return result


class GmailFalso:
    def __init__(self) -> None:
        self.enviados: dict[str, dict[str, str]] = {}
        self.hilos: dict[str, list[Row]] = {}
        self.llamadas = 0

    def enviar(self, mensaje: EmailMessage, hilo: str | None) -> dict[str, str]:
        self.llamadas += 1
        clave = str(mensaje["X-Nexus-Idempotencia"])
        r = {"id": "fake-" + str(self.llamadas), "threadId": hilo or "thread-" + str(self.llamadas)}
        self.enviados[clave] = r
        return r

    def buscar(self, clave: str) -> dict[str, str] | None:
        return self.enviados.get(clave)

    def hilo(self, ident: str) -> list[Row]:
        return self.hilos.get(ident, [])
