"""Permisos por rol. Única fuente: la API los aplica y /api/auth/session los publica para
que el frontend muestre los mismos menús, botones y rutas.

- administrador: todo (gestión de accesos, integraciones, configuración, envíos y cambios
  de campañas, y todas las lecturas).
- usuario (cuenta aprobada por un administrador): lo mismo que el administrador (datos,
  campañas, envíos, integraciones, configuración y auditoría), salvo gestionar accesos:
  no ve ni decide solicitudes de acceso.
"""

from typing import Literal

Rol = Literal["administrador", "usuario"]
ROLES: tuple[Rol, ...] = ("administrador", "usuario")
ETIQUETAS: dict[str, str] = {"administrador": "Administrador", "usuario": "Usuario"}

# Pantallas del frontend (primer segmento de la ruta; "" es el panel general).
PANTALLAS_ADMIN = (
    "",
    "contactos",
    "empresas",
    "descubrimiento",
    "segmentos",
    "secuencias",
    "campanas",
    "respuestas",
    "embudo",
    "informes",
    "configuracion",
    "auditoria",
    "terminal",
    "solicitudes-acceso",
)
PANTALLAS: dict[str, tuple[str, ...]] = {
    "administrador": PANTALLAS_ADMIN,
    "usuario": tuple(p for p in PANTALLAS_ADMIN if p != "solicitudes-acceso"),
}
# Capacidades que el frontend usa para mostrar u ocultar acciones.
CAPACIDADES_ADMIN = (
    "panel:ver",
    "datos:ver",
    "datos:modificar",
    "campanas:modificar",
    "envios:gestionar",
    "integraciones:gestionar",
    "configuracion:gestionar",
    "accesos:gestionar",
    "auditoria:ver",
)
CAPACIDADES: dict[str, tuple[str, ...]] = {
    "administrador": CAPACIDADES_ADMIN,
    "usuario": tuple(c for c in CAPACIDADES_ADMIN if c != "accesos:gestionar"),
}
# Rutas de /api/... reservadas al administrador (gestión de accesos).
SOLO_ADMINISTRADOR = ("solicitudes-acceso",)


def puede_leer(rol: str, ruta: str) -> bool:
    if rol == "administrador":
        return True
    return rol == "usuario" and not ruta.startswith(SOLO_ADMINISTRADOR)


def puede_operar(rol: str) -> bool:
    """Escrituras del negocio: campañas, datos, envíos, integraciones y configuración."""
    return rol in ROLES


def pantalla_permitida(rol: str, enlace: str | None) -> bool:
    if not enlace or not enlace.startswith("/"):
        return False
    primero = enlace.split("?", 1)[0].strip("/").split("/", 1)[0]
    return primero in PANTALLAS.get(rol, ())


def publicos(rol: str) -> dict[str, list[str]]:
    return {
        "pantallas": [p or "panel" for p in PANTALLAS.get(rol, ())],
        "capacidades": list(CAPACIDADES.get(rol, ())),
    }
