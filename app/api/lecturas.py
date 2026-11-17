from typing import Any

from app.db import Conn, Row, obtener, todos, uno
from app.dominio.reglas import coincide
from app.esquemas.serializar import serializar


def evidencias(c: Conn, campo: str, ident: Any) -> list[Row]:
    assert campo in ("empresa_id", "contacto_id", "resultado_id", "propuesta_id")
    return todos(
        c,
        f"SELECT campo,valor,fuente,url,cita,obtenido_en,estado FROM evidencias WHERE {campo}=%s ORDER BY obtenido_en",
        (ident,),
    )


def empresa(c: Conn, r: Row) -> Row:
    r = dict(r)
    r["senales"] = todos(
        c,
        "SELECT tipo,descripcion,url,detectada_en AS fecha FROM senales WHERE empresa_id=%s",
        (r["id"],),
    )
    r["puntaje"] = (
        uno(
            c,
            "SELECT total,incompleto,criterios FROM v_puntaje_actual WHERE empresa_id=%s",
            (r["id"],),
            False,
        )
        or None
    )
    r["evidencias"] = evidencias(c, "empresa_id", r["id"])
    return r


def contacto(c: Conn, r: Row) -> Row:
    r = dict(r)
    r["base_legal"] = uno(
        c,
        "SELECT tipo,texto,prueba,fecha,aprobada_por_admin FROM v_base_legal_vigente WHERE contacto_id=%s",
        (r["id"],),
        False,
    ) or {"tipo": "pendiente", "aprobada_por_admin": False}
    r["evidencias"] = evidencias(c, "contacto_id", r["id"])
    return r


def secuencia(c: Conn, r: Row) -> Row:
    r = dict(r)
    r["ventana_envio"] = {
        "dias": r["ventana_dias"],
        "desde": r["ventana_desde"].strftime("%H:%M"),
        "hasta": r["ventana_hasta"].strftime("%H:%M"),
    }
    r["pasos"] = todos(
        c,
        "SELECT id,orden,asunto,cuerpo,espera_dias_habiles,mismo_hilo,variables FROM pasos WHERE secuencia_id=%s ORDER BY orden",
        (r["id"],),
    )
    return r


def datos_segmento(c: Conn, reglas: list[dict[str, str]]) -> list[Row]:
    empresas = serializar(
        [empresa(c, r) for r in todos(c, "SELECT * FROM empresas WHERE fusionada_en_id IS NULL")]
    )
    contactos = serializar(
        [contacto(c, r) for r in todos(c, "SELECT * FROM contactos WHERE anonimizado_en IS NULL")]
    )
    return [r for r in contactos if coincide(r, empresas, reglas)]


def segmento(c: Conn, r: Row) -> Row:
    return {**r, "tamano": len(datos_segmento(c, r["reglas"]))}


def campana(c: Conn, r: Row) -> Row:
    m = uno(c, "SELECT * FROM v_metricas_campana WHERE campana_id=%s", (r["id"],))
    m["entregados"] = m.pop("entregados_estimados")
    m["aperturas"] = m.pop("aperturas_poco_fiables")
    a = uno(
        c,
        "SELECT v.version,a.huella FROM aprobaciones_campana a JOIN propuesta_versiones v ON v.id=a.propuesta_version_id WHERE a.id=%s AND a.revocada_en IS NULL",
        (r["aprobacion_vigente_id"],),
        False,
    )
    return {
        **r,
        "creada_en": r["creado_en"],
        "metricas": m,
        "version_aprobada": a.get("version"),
        "huella_aprobada": a.get("huella"),
    }


def propuesta(c: Conn, r: Row) -> Row:
    vs = todos(
        c,
        "SELECT version,contenido,creada_en AS fecha,decision,decidida_en,comentario FROM propuesta_versiones WHERE propuesta_id=%s ORDER BY version",
        (r["id"],),
    )
    actual = next((v for v in vs if v["version"] == r["version_actual"]), {})
    return {
        **r,
        "objetivo_id": next(
            (
                r[k]
                for k in (
                    "objetivo_campana_id",
                    "objetivo_segmento_id",
                    "objetivo_empresa_id",
                    "objetivo_contacto_id",
                )
                if r[k]
            ),
            None,
        ),
        "version": r["version_actual"],
        "creada_en": r["creado_en"],
        "versiones": vs,
        "fuentes": evidencias(c, "propuesta_id", r["id"]),
        "decision": {"fecha": actual["decidida_en"], "comentario": actual["comentario"]}
        if actual.get("decision")
        else None,
    }


def respuesta(c: Conn, r: Row) -> Row:
    hilo = todos(
        c,
        "SELECT 'nexus' AS de,enviado_en AS fecha,cuerpo AS texto FROM mensajes WHERE gmail_thread_id=%s AND estado='enviado' UNION ALL SELECT 'contacto',recibida_en,texto FROM respuestas WHERE gmail_thread_id=%s ORDER BY fecha",
        (r["gmail_thread_id"], r["gmail_thread_id"]),
    )
    return {
        **r,
        "fecha": r["recibida_en"],
        "hilo": hilo,
        "clasificacion": r["clasificacion"] or "necesita_revision",
        "confianza": r["confianza"] or 0,
    }


def busqueda(c: Conn, r: Row) -> Row:
    resultados = []
    for x in todos(
        c,
        "SELECT * FROM resultados_descubrimiento WHERE busqueda_id=%s ORDER BY creado_en",
        (r["id"],),
    ):
        resultados.append(
            {
                "id": x["id"],
                "empresa": {
                    "razon_social": x["razon_social"],
                    "sector": x["sector"] or "",
                    "municipio": x["municipio"] or "",
                    "sitio_web": x["sitio_web"],
                },
                "contacto_corporativo": {
                    "correo": x["correo_corporativo"],
                    "telefono": x["telefono"],
                    "whatsapp": x["whatsapp"],
                    "formulario_url": x["formulario_url"],
                },
                "personas": todos(
                    c,
                    "SELECT nombre,cargo,correo,requiere_base_legal FROM personas_descubiertas WHERE resultado_id=%s",
                    (x["id"],),
                ),
                "evidencias": evidencias(c, "resultado_id", x["id"]),
                "posible_duplicado_de": x["posible_duplicado_de"],
                "estado": x["estado"],
            }
        )
    return {
        **r,
        "criterios": {k: r[k] for k in ("consulta", "municipios", "sectores", "tamano")},
        "resultados": resultados,
    }


TABLAS = {
    "empresas": "empresas",
    "contactos": "contactos",
    "segmentos": "segmentos",
    "secuencias": "secuencias",
    "campanas": "campanas",
    "propuestas": "propuestas",
    "respuestas": "respuestas",
    "oportunidades": "oportunidades",
    "supresiones": "supresiones",
    "duplicados": "posibles_duplicados",
    "integraciones": "integraciones",
    "auditoria": "registro_auditoria",
}


def armar(c: Conn, recurso: str, r: Row) -> Row:
    fn = {
        "empresas": empresa,
        "contactos": contacto,
        "segmentos": segmento,
        "secuencias": secuencia,
        "campanas": campana,
        "propuestas": propuesta,
        "respuestas": respuesta,
        "busquedas": busqueda,
    }.get(recurso)
    if fn:
        return fn(c, r)
    if recurso == "integraciones":
        return {k: r[k] for k in ("id", "nombre", "estado", "detalle", "ultima_sincronizacion")}
    if recurso == "auditoria":
        return {**r, "id": str(r["id"])}
    if recurso == "oportunidades":
        return {**r, "creada_en": r["creado_en"]}
    if recurso == "supresiones":
        return {**r, "fecha": r["creado_en"]}
    return r


def lista(c: Conn, recurso: str, ident: str | None = None) -> Any:
    tabla = TABLAS[recurso]
    if ident:
        r = obtener(c, tabla, ident)
        if recurso == "empresas" and r["fusionada_en_id"]:
            from fastapi import HTTPException

            raise HTTPException(404, "Empresa no encontrada")
        return armar(c, recurso, r)
    where = {
        "empresas": " WHERE fusionada_en_id IS NULL",
        "duplicados": " WHERE estado='pendiente'",
    }.get(recurso, "")
    order = {
        "auditoria": " ORDER BY fecha DESC LIMIT 500",
        "propuestas": " ORDER BY (estado='pendiente') DESC,creado_en DESC",
        "respuestas": " ORDER BY (estado='pendiente') DESC,recibida_en DESC",
    }.get(recurso, "")
    return [armar(c, recurso, r) for r in todos(c, f"SELECT * FROM {tabla}{where}{order}")]
