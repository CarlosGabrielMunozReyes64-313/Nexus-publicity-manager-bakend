"""Permisos por rol. Única fuente: la API los aplica y /api/auth/session los publica para
que el frontend muestre los mismos menús, botones y rutas.

- administrador: todo (gestión de accesos, integraciones, configuración, envíos y cambios
  de campañas, y todas las lecturas).
- usuario: solo lectura del panel (indicadores agregados y embudo) y su propia bandeja de
  notificaciones. Ninguna escritura sobre datos del negocio.
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
    "usuario": ("",),
}
# Capacidades que el frontend usa para mostrar u ocultar acciones.
CAPACIDADES: dict[str, tuple[str, ...]] = {
    "administrador": (
        "panel:ver",
        "datos:ver",
        "datos:modificar",
        "campanas:modificar",
        "envios:gestionar",
        "integraciones:gestionar",
        "configuracion:gestionar",
        "accesos:gestionar",
        "auditoria:ver",
    ),
    "usuario": ("panel:ver",),
}
# Lecturas GET de /api/... que el rol «usuario» puede hacer (ruta exacta, sin consulta).
LECTURAS_USUARIO = frozenset({"panel/resumen", "embudo"})


def puede_leer(rol: str, ruta: str) -> bool:
    return rol == "administrador" or (rol == "usuario" and ruta in LECTURAS_USUARIO)


def puede_escribir(rol: str) -> bool:
    return rol == "administrador"


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
