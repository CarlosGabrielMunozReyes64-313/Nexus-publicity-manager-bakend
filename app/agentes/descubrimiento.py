"""Programación e incorporación opcional; nunca aprueba contactos ni envíos."""

import re
from datetime import datetime
from typing import Any

from psycopg.errors import UniqueViolation

from app.api import datos
from app.config import config
from app.db import Conn, Row, actualizar, encolar, insertar, uno
from app.esquemas.entradas import Empresa

DEPARTAMENTOS = (
    "Amazonas",
    "Antioquia",
    "Arauca",
    "Atlántico",
    "Bogotá D.C.",
    "Bolívar",
    "Boyacá",
    "Caldas",
    "Caquetá",
    "Casanare",
    "Cauca",
    "Cesar",
    "Chocó",
    "Córdoba",
    "Cundinamarca",
    "Guainía",
    "Guaviare",
    "Huila",
    "La Guajira",
    "Magdalena",
    "Meta",
    "Nariño",
    "Norte de Santander",
    "Putumayo",
    "Quindío",
    "Risaralda",
    "San Andrés y Providencia",
    "Santander",
    "Sucre",
    "Tolima",
    "Valle del Cauca",
    "Vaupés",
    "Vichada",
)
GENERICO = re.compile(
    r"^(info|informacion|contacto|contactenos|ventas|comercial|servicioalcliente|atencionalcliente|administracion|gerencia|recepcion|soporte|reservas|cotizaciones)$"
)


def programar_descubrimiento(c: Conn, now: datetime) -> None:
    cfg = config()
    if not cfg.descubrimiento_automatico or now.hour < cfg.descubrimiento_hora:
        return
    if cfg.agente_proveedor != "gemini" or not cfg.gemini_api_key.get_secret_value():
        return
    day = now.date().isoformat()
    # Una sola planificación diaria, incluso con varios trabajadores o cambios de límite.
    c.execute("SELECT pg_advisory_xact_lock(70740)")
    if uno(
        c,
        "SELECT id FROM tareas WHERE clave_unica=%s",
        ("descubrimiento-auto:" + day + ":0",),
        False,
    ):
        return
    for batch, offset in enumerate(range(0, cfg.descubrimiento_limite_diario, 5)):
        cantidad = min(5, cfg.descubrimiento_limite_diario - offset)
        region = DEPARTAMENTOS[(now.date().toordinal() * 4 + batch) % len(DEPARTAMENTOS)]
        b = insertar(
            c,
            "busquedas_descubrimiento",
            {
                "consulta": f"Encuentra hasta {cantidad} empresas en {region}, Colombia, de cualquier sector. "
                "Prioriza pequeñas y medianas empresas con sitio oficial y correo corporativo genérico público. "
                "Incluye evidencia literal del nombre, correo, municipio y país Colombia. No inventes datos.",
                "municipios": [],
                "sectores": [],
            },
        )
        encolar(
            c,
            "descubrir_empresas",
            {
                "busquedaId": str(b["id"]),
                "autoImportar": True,
                "limite": cantidad,
                "fechaProgramada": day,
            },
            f"descubrimiento-auto:{day}:{batch}",
            8,
        )


def corroborados(items: list[Row]) -> dict[str, Any]:
    """Una cita existente no basta: debe contener también el valor que se atribuye."""
    from app.agentes.lector_web import normalizar

    return {
        d["campo"]: d["valor"]
        for d in items
        if d["estado"] == "verificado"
        and d["valor"]
        and normalizar(str(d["valor"])).casefold() in normalizar(d["cita"]).casefold()
    }


def importar(c: Conn, r: Row, evidencias: list[Row]) -> bool:
    verified = corroborados(evidencias)
    if (
        str(verified.get("pais", "")).casefold() != "colombia"
        or verified.get("razonSocial") != r["razon_social"]
    ):
        return False
    c.execute("SELECT pg_advisory_xact_lock(70741)")
    n = uno(
        c,
        """SELECT count(*) AS n FROM resultados_descubrimiento r
        JOIN tareas t ON t.datos->>'busquedaId'=r.busqueda_id::text
        WHERE t.datos->>'autoImportar'='true' AND r.estado='aceptado'
        AND (r.decidido_en AT TIME ZONE 'America/Bogota')::date=
            (now() AT TIME ZONE 'America/Bogota')::date""",
    )["n"]
    if n >= config().descubrimiento_limite_diario:
        return False
    duplicate = uno(
        c,
        """SELECT id FROM empresas WHERE fusionada_en_id IS NULL AND
        (dominio=%s OR (lower(razon_social)=lower(%s) AND lower(municipio)=lower(%s))) LIMIT 1""",
        (r["dominio"], r["razon_social"], verified.get("municipio", "")),
        False,
    )
    if duplicate:
        actualizar(
            c, "resultados_descubrimiento", str(r["id"]), {"posible_duplicado_de": duplicate["id"]}
        )
        return False
    # Savepoint: una alta concurrente no debe cancelar el resto del lote.
    try:
        with c.transaction():
            fields = Empresa.model_validate(
                {
                    "razonSocial": r["razon_social"],
                    "sitioWeb": r["sitio_web"],
                    "dominio": r["dominio"],
                    "municipio": verified.get("municipio", ""),
                    "sector": verified.get("sector", "Otro"),
                }
            ).model_dump()
            e = insertar(c, "empresas", fields)
            for item in evidencias:
                insertar(c, "evidencias", {"empresa_id": e["id"], **item})
            email = str(verified.get("correoCorporativo", "")).strip().lower()
            local, sep, domain = email.partition("@")
            public = bool(sep and GENERICO.fullmatch(local) and domain == r["dominio"])
            suppressed = uno(
                c,
                "SELECT id FROM supresiones WHERE correo=%s OR dominio=%s LIMIT 1",
                (email, domain),
                False,
            )
            existing = uno(c, "SELECT id FROM contactos WHERE correo=%s", (email,), False)
            if public and not suppressed and not existing:
                ct = datos.guardar_contacto(
                    c,
                    {
                        "empresaId": str(e["id"]),
                        "nombre": "Contacto corporativo",
                        "correo": email,
                        "origen": "descubrimiento",
                        "baseLegal": {"tipo": "pendiente"},
                    },
                )
                for item in evidencias:
                    if item["campo"] == "correoCorporativo":
                        insertar(c, "evidencias", {"contacto_id": ct["id"], **item})
            actualizar(
                c,
                "resultados_descubrimiento",
                str(r["id"]),
                {
                    "estado": "aceptado",
                    "empresa_creada_id": e["id"],
                    "decidido_en": datos.ahora(),
                },
            )
            encolar(c, "calcular_puntaje", {"empresaId": str(e["id"])})
        return True
    except UniqueViolation:
        return False
