"""Programación e incorporación opcional; nunca aprueba contactos ni envíos."""

import re
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.api import datos
from app.config import config
from app.db import Conn, Row, actualizar, auditar, encolar, insertar, todos, uno
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


def importar(c: Conn, r: Row, evidencias: list[Row], tope_diario: bool = True) -> bool:
    """Crea la empresa (y su contacto corporativo si publica un correo genérico) sin revisión.

    ``tope_diario``: el descubrimiento automático por variables de entorno respeta
    DESCUBRIMIENTO_LIMITE_DIARIO. La meta diaria lleva su propia cuenta (``aceptar_meta``).
    """
    verified = corroborados(evidencias)
    if (
        str(verified.get("pais", "")).casefold() != "colombia"
        or verified.get("razonSocial") != r["razon_social"]
    ):
        return False
    c.execute("SELECT pg_advisory_xact_lock(70741)")
    n = (
        0
        if not tope_diario
        else uno(
            c,
            """SELECT count(*) AS n FROM resultados_descubrimiento r
        JOIN tareas t ON t.datos->>'busquedaId'=r.busqueda_id::text
        WHERE t.datos->>'autoImportar'='true' AND r.estado='aceptado'
        AND (r.decidido_en AT TIME ZONE 'America/Bogota')::date=
            (now() AT TIME ZONE 'America/Bogota')::date""",
        )["n"]
    )
    if tope_diario and n >= config().descubrimiento_limite_diario:
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


# --- Meta diaria de contactos (migración 0007) -------------------------------------------

TAMANOS = ("", "1-10", "11-50", "51-200", "200+")
META_FALLOS_SEGUIDOS = 3


def leer_meta(c: Conn) -> dict[str, Any]:
    fila = uno(c, "SELECT valor FROM configuracion WHERE clave='descubrimiento'", (), False)
    valor = dict(fila["valor"]) if fila else {}
    return {
        "activa": bool(valor.get("activa", False)),
        "metaDiaria": int(valor.get("metaDiaria", 20)),
        "maxBusquedas": int(valor.get("maxBusquedas", 40)),
        "tipos": list(valor.get("tipos", [])),
        "municipios": list(valor.get("municipios", [])),
        "sectores": list(valor.get("sectores", [])),
        "tamano": str(valor.get("tamano", "")),
    }


def _lista(body: dict[str, Any], campo: str, maximo: int) -> list[str]:
    valor = body.get(campo, [])
    if not isinstance(valor, list) or not all(isinstance(x, str) for x in valor):
        raise HTTPException(400, f"«{campo}» debe ser una lista de textos")
    limpios: list[str] = []
    for x in valor:
        texto = " ".join(x.split())
        if not 1 <= len(texto) <= 120:
            raise HTTPException(400, f"Cada valor de «{campo}» debe tener entre 1 y 120 caracteres")
        if texto.casefold() not in {y.casefold() for y in limpios}:
            limpios.append(texto)
    if len(limpios) > maximo:
        raise HTTPException(400, f"«{campo}» admite como máximo {maximo} valores")
    return limpios


def _entero(body: dict[str, Any], campo: str, minimo: int, maximo: int) -> int:
    valor = body.get(campo)
    if isinstance(valor, bool) or not isinstance(valor, int) or not minimo <= valor <= maximo:
        raise HTTPException(400, f"«{campo}» debe ser un número entero entre {minimo} y {maximo}")
    return valor


def guardar_meta(c: Conn, body: Any, admin: str) -> dict[str, Any]:
    """PUT /descubrimiento/meta: meta, tope de búsquedas y filtros que usará cada día."""
    if not isinstance(body, dict):
        raise HTTPException(400, "Datos de la meta no válidos")
    activa = body.get("activa")
    if not isinstance(activa, bool):
        raise HTTPException(400, "Indique si la meta queda encendida (sí o no)")
    tamano = body.get("tamano", "")
    if tamano not in TAMANOS:
        raise HTTPException(400, "Tamaño no válido")
    valor = {
        "activa": activa,
        "metaDiaria": _entero(body, "metaDiaria", 1, 200),
        "maxBusquedas": _entero(body, "maxBusquedas", 1, 500),
        "tipos": _lista(body, "tipos", 100),
        "municipios": _lista(body, "municipios", 200),
        "sectores": _lista(body, "sectores", 50),
        "tamano": tamano,
    }
    if activa and not valor["tipos"]:
        raise HTTPException(400, "Elija al menos un tipo de empresa para buscar cada día")
    c.execute(
        "UPDATE configuracion SET valor=%s, actualizado_por=%s WHERE clave='descubrimiento'",
        (Jsonb(valor), admin),
    )
    auditar(
        c,
        "meta_descubrimiento_" + ("encendida" if activa else "guardada"),
        "configuracion",
        "descubrimiento",
    )
    return {"ok": True}


def contactos_hoy(c: Conn) -> int:
    """Contactos nuevos de Descubrimiento hoy (hora de Colombia), aceptados a mano o solos."""
    return int(
        uno(
            c,
            """SELECT count(*) AS n FROM contactos WHERE origen='descubrimiento'
               AND (creado_en AT TIME ZONE 'America/Bogota')::date=
                   (now() AT TIME ZONE 'America/Bogota')::date""",
        )["n"]
    )


def _tareas_meta_hoy(c: Conn, dia: str) -> list[Row]:
    return todos(
        c,
        """SELECT id,estado,ultimo_error,datos FROM tareas
           WHERE tipo='descubrir_empresas' AND datos->>'meta'='true' AND datos->>'fecha'=%s
           ORDER BY creada_en""",
        (dia,),
    )


def combinaciones(meta: dict[str, Any]) -> list[tuple[str, str]]:
    """Un tipo y un municipio por búsqueda: así cada búsqueda lee páginas de ese caso."""
    return [(t, m) for t in meta["tipos"] for m in (meta["municipios"] or [""])]


def estado_meta(c: Conn, now: datetime) -> dict[str, Any]:
    """GET /descubrimiento/meta: la configuración y cómo va hoy."""
    from app.dominio.reglas import BOGOTA

    meta = leer_meta(c)
    local = now.astimezone(BOGOTA)
    tareas = _tareas_meta_hoy(c, local.date().isoformat())
    hechos = contactos_hoy(c)
    en_curso = next((t for t in tareas if t["estado"] in ("pendiente", "en_proceso")), None)
    fallos = [t for t in tareas[-META_FALLOS_SEGUIDOS:] if t["estado"] == "fallida"]
    cfg = config()
    if not meta["activa"]:
        texto = "Apagada. Solo se busca cuando usted inicia una búsqueda."
    elif hechos >= meta["metaDiaria"]:
        texto = f"Meta cumplida por hoy: {hechos} contactos nuevos."
    elif local.hour < cfg.descubrimiento_hora:
        texto = f"Empieza hoy a las {cfg.descubrimiento_hora}:00 (hora de Colombia)."
    elif en_curso:
        b = uno(
            c,
            "SELECT consulta,municipios,error FROM busquedas_descubrimiento WHERE id=%s",
            (en_curso["datos"]["busquedaId"],),
            False,
        )
        que = (
            f"«{str(b['consulta']).split(':', 1)[-1].strip()}»"
            + (f" en {', '.join(b['municipios'])}" if b and b["municipios"] else "")
            if b
            else "una búsqueda"
        )
        texto = f"Buscando {que}." + (f" {b['error']}" if b and b["error"] else "")
    elif len(fallos) == META_FALLOS_SEGUIDOS:
        texto = (
            f"En pausa: las últimas {META_FALLOS_SEGUIDOS} búsquedas fallaron "
            f"({fallos[-1]['ultimo_error'] or 'sin detalle'}). Vuelva a guardar la meta para "
            "reintentar."
        )
    elif len(tareas) >= meta["maxBusquedas"]:
        texto = f"Tope de {meta['maxBusquedas']} búsquedas alcanzado por hoy. Sigue mañana."
    else:
        texto = "Preparando la siguiente búsqueda."
    return {
        **meta,
        "hoy": {
            "contactos": hechos,
            "busquedas": len(tareas),
            "en_curso": bool(en_curso),
        },
        "combinaciones": len(combinaciones(meta)),
        "creditos_por_busqueda": cfg.tavily_consultas_max,
        "estado": texto,
    }


def programar_meta(c: Conn, now: datetime) -> None:
    """Lanza la siguiente búsqueda de la meta: una a la vez, hasta la meta o el tope del día."""
    from app.agentes.ejecutor import proveedor_ia_listo
    from app.tareas.bitacora import evento

    meta = leer_meta(c)
    if not meta["activa"] or now.hour < config().descubrimiento_hora:
        return
    combos = combinaciones(meta)
    if not combos:
        return
    integraciones = {
        r["id"] for r in todos(c, "SELECT id FROM integraciones WHERE estado='conectada'")
    }
    if not proveedor_ia_listo(integraciones):
        return
    dia = now.date().isoformat()
    c.execute("SELECT pg_advisory_xact_lock(70742)")
    tareas = _tareas_meta_hoy(c, dia)
    if any(t["estado"] in ("pendiente", "en_proceso") for t in tareas):
        return
    hechos = contactos_hoy(c)
    if hechos >= meta["metaDiaria"]:
        if tareas and not tareas[-1]["datos"].get("_metaCumplida"):
            c.execute(
                "UPDATE tareas SET datos=datos||'{\"_metaCumplida\":true}'::jsonb WHERE id=%s",
                (tareas[-1]["id"],),
            )
            evento(c, f"Meta diaria cumplida: {hechos} de {meta['metaDiaria']} contactos.", "exito")
        return
    if len(tareas) >= meta["maxBusquedas"]:
        return
    ultimas = tareas[-META_FALLOS_SEGUIDOS:]
    if len(ultimas) == META_FALLOS_SEGUIDOS and all(t["estado"] == "fallida" for t in ultimas):
        return
    total = uno(
        c,
        "SELECT count(*) AS n FROM tareas WHERE tipo='descubrir_empresas' AND datos->>'meta'='true'",
    )["n"]
    tipo, municipio = combos[int(total) % len(combos)]
    b = insertar(
        c,
        "busquedas_descubrimiento",
        {
            "consulta": f"Busca empresas en Colombia: {tipo}",
            "municipios": [municipio] if municipio else [],
            "sectores": meta["sectores"],
            "tamano": meta["tamano"] or None,
        },
    )
    n = len(tareas) + 1
    encolar(
        c,
        "descubrir_empresas",
        {"busquedaId": str(b["id"]), "meta": True, "fecha": dia},
        f"meta:{dia}:{n}",
        8,
    )
    lugar = f" en {municipio}" if municipio else ""
    evento(
        c,
        f"Meta diaria: {hechos} de {meta['metaDiaria']} contactos. Búsqueda {n} de "
        f"{meta['maxBusquedas']}: «{tipo}»{lugar}.",
    )


def aceptar_meta(c: Conn, r: Row, evidencias: list[Row]) -> bool:
    """Acepta sola una empresa de la meta si nombre, país y correo genérico del propio dominio
    están corroborados en su sitio. Lo demás queda pendiente de revisión."""
    verified = corroborados(evidencias)
    correo = str(verified.get("correoCorporativo", "")).strip().lower()
    local, arroba, dominio = correo.partition("@")
    if not (
        arroba
        and GENERICO.fullmatch(local)
        and dominio == r["dominio"]
        and correo == str(r["correo_corporativo"] or "").strip().lower()
    ):
        return False
    c.execute("SELECT pg_advisory_xact_lock(70741)")
    if contactos_hoy(c) >= leer_meta(c)["metaDiaria"]:
        return False
    return importar(c, r, evidencias, tope_diario=False)
