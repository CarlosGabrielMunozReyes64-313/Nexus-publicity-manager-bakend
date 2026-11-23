import re
from typing import Any, Protocol

import dns.exception
import dns.resolver
import httpx

from app.config import config
from app.db import Conn, insertar, obtener, uno


class Verificador(Protocol):
    def consultar(self, correo: str) -> dict[str, Any]: ...


class HunterReal:
    def consultar(self, correo: str) -> dict[str, Any]:
        key = config().hunter_api_key.get_secret_value()
        if not key:
            return {}
        r = httpx.get(
            "https://api.hunter.io/v2/email-verifier",
            params={"email": correo, "api_key": key},
            timeout=15,
        )
        if r.status_code in (401, 403, 429):
            return {}
        r.raise_for_status()
        result: dict[str, Any] = r.json().get("data", {})
        return result


class HunterFalso:
    def __init__(self, resultado: dict[str, Any]) -> None:
        self.resultado = resultado

    def consultar(self, correo: str) -> dict[str, Any]:
        return self.resultado


def traducir(data: dict[str, Any], correo: str) -> str:
    role = correo.split("@")[0].lower() in (
        "info",
        "ventas",
        "contacto",
        "admin",
        "soporte",
        "comercial",
    )
    if data.get("status") == "invalid":
        return "invalido"
    if data.get("accept_all") or data.get("status") == "accept_all":
        return "catch_all"
    if not data:
        return "desconocido"
    if role or data.get("disposable") or data.get("status") == "webmail":
        return "riesgoso"
    return "valido" if data.get("status") == "valid" else "desconocido"


def dns_valido(dominio: str) -> bool | None:
    try:
        mx = dns.resolver.resolve(dominio, "MX", lifetime=5)
        return any(str(r.exchange) != "." for r in mx)
    except dns.resolver.NoAnswer:
        try:
            return bool(dns.resolver.resolve(dominio, "A", lifetime=5))
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            return False
        except dns.exception.DNSException:
            return None
    except dns.resolver.NXDOMAIN:
        return False
    except dns.exception.DNSException:
        return None


def verificar(c: Conn, ident: str, proveedor: Verificador | None = None) -> None:
    ct = obtener(c, "contactos", ident, True)
    correo = ct["correo"]
    if not correo:
        return
    estado = "desconocido"
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", correo):
        estado = "invalido"
    elif proveedor is not None:
        estado = traducir(proveedor.consultar(correo), correo)
    else:
        dns = dns_valido(correo.split("@")[1])
        if dns is False:
            estado = "invalido"
        elif dns and obtener(c, "integraciones", "verificador")["estado"] == "conectada":
            estado = traducir(HunterReal().consultar(correo), correo)
    insertar(
        c,
        "verificaciones_correo",
        {"contacto_id": ident, "correo": correo, "resultado": estado, "proveedor": "dns_mx+hunter"},
    )
    from app.api.datos import aprobar_automatica, avanzar

    aprobar_automatica(c, ident)
    if uno(c, "SELECT motivo_no_elegible(%s) AS motivo", (ident,))["motivo"] is None:
        avanzar(c, ident, "contactable", "contactable")
