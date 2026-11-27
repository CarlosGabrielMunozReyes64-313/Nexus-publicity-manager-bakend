"""Lecturas para la API.

Las fichas individuales incluyen todas sus evidencias. Los listados se arman con
consultas agrupadas (una por relación, no una por fila) y dejan las evidencias
completas para la ficha de detalle.
"""

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException

from app.db import Conn, Row, obtener, todos, uno
from app.dominio.reglas import coincide_indexado, ventanas_compatibles
from app.esquemas.serializar import serializar
from app.motor.pausas import MOTIVO_VENTANA

CAMPOS_EVIDENCIA = "campo,valor,fuente,url,cita,obtenido_en,estado"


def _ids(filas: Iterable[Row], clave: str = "id") -> list[str]:
    return [str(r[clave]) for r in filas]


def evidencias(c: Conn, campo: str, ident: Any) -> list[Row]:
    assert campo in ("empresa_id", "contacto_id", "resultado_id", "propuesta_id")
    return todos(
        c,
        f"SELECT {CAMPOS_EVIDENCIA} FROM evidencias WHERE {campo}=%s ORDER BY obtenido_en",
        (ident,),
    )


def evidencias_de(c: Conn, campo: str, ids: list[str]) -> dict[str, list[Row]]:
    assert campo in ("empresa_id", "contacto_id", "resultado_id", "propuesta_id")
    out: dict[str, list[Row]] = defaultdict(list)
    if not ids:
        return out
    for r in todos(
        c,
        f"SELECT {campo} AS dueno,{CAMPOS_EVIDENCIA} FROM evidencias WHERE {campo}=ANY(%s::uuid[]) ORDER BY obtenido_en",
        (ids,),
    ):
        out[str(r.pop("dueno"))].append(r)
    return out


def _senales_de(c: Conn, ids: list[str]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = defaultdict(list)
    if ids:
        for r in todos(
            c,
            "SELECT empresa_id,tipo,descripcion,url,detectada_en AS fecha FROM senales WHERE empresa_id=ANY(%s::uuid[]) ORDER BY detectada_en",
            (ids,),
        ):
            out[str(r.pop("empresa_id"))].append(r)
    return out


def _puntajes_de(c: Conn, ids: list[str]) -> dict[str, Row]:
    if not ids:
        return {}
    return {
        str(r.pop("empresa_id")): r
        for r in todos(
            c,
            "SELECT empresa_id,total,incompleto,criterios FROM v_puntaje_actual WHERE empresa_id=ANY(%s::uuid[])",
            (ids,),
        )
    }


def _bases_de(c: Conn, ids: list[str]) -> dict[str, Row]:
    if not ids:
        return {}
    return {
        str(r.pop("contacto_id")): r
        for r in todos(
            c,
            "SELECT contacto_id,tipo,texto,prueba,fecha,aprobada_por_admin FROM v_base_legal_vigente WHERE contacto_id=ANY(%s::uuid[])",
            (ids,),
        )
    }


def empresa(c: Conn, r: Row) -> Row:
    return empresas(c, [r], con_evidencias=True)[0]


def empresas(c: Conn, filas: list[Row], con_evidencias: bool = False) -> list[Row]:
    ids = _ids(filas)
    senales = _senales_de(c, ids)
    puntajes = _puntajes_de(c, ids)
    ev = evidencias_de(c, "empresa_id", ids) if con_evidencias else {}
    out = []
    for r in filas:
        r = dict(r)
        clave = str(r["id"])
        r["senales"] = senales.get(clave, [])
        r["puntaje"] = puntajes.get(clave)
        r["evidencias"] = ev.get(clave, [])
        out.append(r)
    return out


def contacto(c: Conn, r: Row) -> Row:
    return contactos(c, [r], con_evidencias=True)[0]


def contactos(c: Conn, filas: list[Row], con_evidencias: bool = False) -> list[Row]:
    ids = _ids(filas)
    bases = _bases_de(c, ids)
    ev = evidencias_de(c, "contacto_id", ids) if con_evidencias else {}
    out = []
    for r in filas:
        r = dict(r)
        clave = str(r["id"])
        r["base_legal"] = bases.get(clave) or {"tipo": "pendiente", "aprobada_por_admin": False}
        r["evidencias"] = ev.get(clave, [])
        out.append(r)
    return out


def secuencia(c: Conn, r: Row) -> Row:
    return secuencias(c, [r])[0]


def secuencias(c: Conn, filas: list[Row]) -> list[Row]:
    pasos: dict[str, list[Row]] = defaultdict(list)
    ids = _ids(filas)
    if ids:
        for p in todos(
            c,
            "SELECT secuencia_id,id,orden,asunto,cuerpo,espera_dias_habiles,mismo_hilo,variables,formato,cuerpo_html FROM pasos WHERE secuencia_id=ANY(%s::uuid[]) ORDER BY orden",
            (ids,),
        ):
            pasos[str(p.pop("secuencia_id"))].append(p)
    out = []
    for r in filas:
        r = dict(r)
        r["ventana_envio"] = {
            "dias": r["ventana_dias"],
            "desde": r["ventana_desde"].strftime("%H:%M"),
            "hasta": r["ventana_hasta"].strftime("%H:%M"),
        }
        r["pasos"] = pasos.get(str(r["id"]), [])
        out.append(r)
    return out


def base_segmentos(c: Conn) -> tuple[dict[str, Row], list[Row]]:
    """Datos mínimos para evaluar reglas de segmento (consultas agrupadas, no por fila).

    Incluye atributos guardados y comportamientos respaldados por eventos: clic humano
    (no de escáner), respuesta recibida, correo enviado y pertenencia a listas activas.
    """
    empresas_idx: dict[str, Row] = {}
    for e in todos(
        c,
        """SELECT e.id,e.municipio,e.sector,e.tamano,e.estado_cuenta,p.total FROM empresas e
        LEFT JOIN v_puntaje_actual p ON p.empresa_id=e.id WHERE e.fusionada_en_id IS NULL""",
    ):
        dato: Row = {k: e[k] for k in ("municipio", "sector", "tamano")}
        dato["estadoCuenta"] = e["estado_cuenta"]
        if e["total"] is not None:
            dato["puntaje"] = {"total": e["total"]}
        empresas_idx[str(e["id"])] = dato
    listas: dict[str, set[str]] = defaultdict(set)
    for r in todos(
        c,
        """SELECT lm.contacto_id,lm.lista_id FROM lista_miembros lm
        JOIN listas l ON l.id=lm.lista_id WHERE lm.activo AND l.archivada_en IS NULL""",
    ):
        listas[str(r["contacto_id"])].add(str(r["lista_id"]))
    clics = {
        str(r["cid"])
        for r in todos(
            c,
            """SELECT DISTINCT coalesce(e.contacto_id,m.contacto_id) AS cid FROM eventos_correo e
            LEFT JOIN mensajes m ON m.id=e.mensaje_id WHERE e.tipo='clic' AND NOT e.automatico""",
        )
        if r["cid"]
    }
    respondieron = {
        str(r["contacto_id"]) for r in todos(c, "SELECT DISTINCT contacto_id FROM respuestas")
    }
    recibieron = {
        str(r["contacto_id"])
        for r in todos(c, "SELECT DISTINCT contacto_id FROM mensajes WHERE estado='enviado'")
    }
    personas = [
        {
            "id": str(r["id"]),
            "empresaId": str(r["empresa_id"]),
            "etapa": r["etapa"],
            "origen": r["origen"],
            "cargo": r["cargo"],
            "correo": r["correo"],
            "estadoCorreo": r["estado_correo"],
            "baseLegal": {"aprobadaPorAdmin": bool(r["aprobada"])},
            "listas": listas.get(str(r["id"]), set()),
            "hizoClic": str(r["id"]) in clics,
            "respondio": str(r["id"]) in respondieron,
            "recibioCorreo": str(r["id"]) in recibieron,
        }
        for r in todos(
            c,
            """SELECT c.id,c.empresa_id,c.etapa,c.origen,c.cargo,c.correo::text AS correo,
            c.estado_correo,coalesce(b.aprobada_por_admin,false) AS aprobada
            FROM contactos c LEFT JOIN v_base_legal_vigente b ON b.contacto_id=c.id
            WHERE c.anonimizado_en IS NULL ORDER BY c.creado_en, c.id""",
        )
    ]
    return empresas_idx, personas


def ids_segmento(
    c: Conn,
    reglas: list[Any],
    base: tuple[dict[str, Row], list[Row]] | None = None,
) -> list[str]:
    empresas_idx, personas = base or base_segmentos(c)
    return [p["id"] for p in personas if coincide_indexado(p, empresas_idx, reglas)]


def datos_segmento(c: Conn, reglas: list[Any]) -> list[Row]:
    """Contactos completos que cumplen las reglas (para muestras y agentes)."""
    ids = ids_segmento(c, reglas)
    if not ids:
        return []
    filas = todos(c, "SELECT * FROM contactos WHERE id=ANY(%s::uuid[]) ORDER BY creado_en", (ids,))
    return list(serializar(contactos(c, filas)))


def segmento(c: Conn, r: Row, base: tuple[dict[str, Row], list[Row]] | None = None) -> Row:
    return {**r, "tamano": len(ids_segmento(c, r["reglas"], base))}


def campana(c: Conn, r: Row) -> Row:
    return campanas(c, [r])[0]


def campanas(c: Conn, filas: list[Row]) -> list[Row]:
    ids = _ids(filas)
    metricas = {
        str(m.pop("campana_id")): m
        for m in (
            todos(c, "SELECT * FROM v_metricas_campana WHERE campana_id=ANY(%s::uuid[])", (ids,))
            if ids
            else []
        )
    }
    aprobaciones_ids = [
        str(r["aprobacion_vigente_id"]) for r in filas if r["aprobacion_vigente_id"]
    ]
    aprobaciones = {
        str(a.pop("id")): a
        for a in (
            todos(
                c,
                """SELECT a.id,v.version,a.huella,a.contenido_congelado->'secuencia'->'ventanaEnvio' AS ventana
                FROM aprobaciones_campana a JOIN propuesta_versiones v ON v.id=a.propuesta_version_id
                WHERE a.id=ANY(%s::uuid[]) AND a.revocada_en IS NULL""",
                (aprobaciones_ids,),
            )
            if aprobaciones_ids
            else []
        )
    }
    contexto = (
        uno(
            c,
            """SELECT (SELECT valor->'ventana' FROM configuracion WHERE clave='limites') AS ventana,
            (SELECT estado FROM integraciones WHERE id='gmail') AS gmail""",
        )
        if filas
        else {}
    )
    out = []
    for r in filas:
        m = dict(metricas[str(r["id"])])
        m["entregados"] = m.pop("entregados_estimados")
        m["aperturas"] = m.pop("aperturas_poco_fiables")
        a = aprobaciones.get(str(r["aprobacion_vigente_id"]), {})
        motivo = None
        if (
            a.get("ventana")
            and contexto.get("ventana")
            and r["estado"] in ("aprobada", "activa", "pausada")
            and not ventanas_compatibles(a["ventana"], contexto["ventana"])
        ):
            motivo = MOTIVO_VENTANA
        elif r["estado"] == "pausada" and contexto.get("gmail") != "conectada":
            motivo = "Gmail no está conectado: reconéctelo en Configuración antes de reanudar."
        out.append(
            {
                **r,
                "creada_en": r["creado_en"],
                "metricas": m,
                "version_aprobada": a.get("version"),
                "huella_aprobada": a.get("huella"),
                "motivo_pausa": motivo,
            }
        )
    return out


def propuesta(c: Conn, r: Row) -> Row:
    return propuestas(c, [r])[0]


def propuestas(c: Conn, filas: list[Row]) -> list[Row]:
    ids = _ids(filas)
    versiones: dict[str, list[Row]] = defaultdict(list)
    if ids:
        for v in todos(
            c,
            "SELECT propuesta_id,version,contenido,creada_en AS fecha,autor,decision,decidida_en,comentario FROM propuesta_versiones WHERE propuesta_id=ANY(%s::uuid[]) ORDER BY version",
            (ids,),
        ):
            versiones[str(v.pop("propuesta_id"))].append(v)
    fuentes = evidencias_de(c, "propuesta_id", ids)
    # Rutas de correo: campaña y segmento en una sola consulta (para la lista).
    rutas: dict[str, Row] = {}
    campanas_ids = [str(r["objetivo_campana_id"]) for r in filas if r.get("objetivo_campana_id")]
    if campanas_ids:
        for x in todos(
            c,
            """SELECT cam.id,cam.nombre AS campana_nombre,cam.estado AS campana_estado,
                      s.id AS segmento_id,s.nombre AS segmento_nombre
               FROM campanas cam JOIN segmentos s ON s.id=cam.segmento_id
               WHERE cam.id=ANY(%s::uuid[])""",
            (campanas_ids,),
        ):
            rutas[str(x.pop("id"))] = x
    out = []
    for r in filas:
        vs = versiones.get(str(r["id"]), [])
        actual = next((v for v in vs if v["version"] == r["version_actual"]), {})
        out.append(
            {
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
                "fuentes": fuentes.get(str(r["id"]), []),
                "decision": {"fecha": actual["decidida_en"], "comentario": actual["comentario"]}
                if actual.get("decision")
                else None,
                "ruta": rutas.get(str(r.get("objetivo_campana_id"))),
            }
        )
    return out


def respuesta(c: Conn, r: Row) -> Row:
    return respuestas(c, [r])[0]


def respuestas(c: Conn, filas: list[Row]) -> list[Row]:
    hilos = sorted({str(r["gmail_thread_id"]) for r in filas})
    por_hilo: dict[str, list[Row]] = defaultdict(list)
    if hilos:
        for h in todos(
            c,
            """SELECT gmail_thread_id AS hilo,'nexus' AS de,enviado_en AS fecha,cuerpo AS texto
            FROM mensajes WHERE gmail_thread_id=ANY(%s) AND estado='enviado'
            UNION ALL SELECT gmail_thread_id,'contacto',recibida_en,texto
            FROM respuestas WHERE gmail_thread_id=ANY(%s) ORDER BY fecha""",
            (hilos, hilos),
        ):
            por_hilo[str(h.pop("hilo"))].append(h)
    return [
        {
            **r,
            "fecha": r["recibida_en"],
            "hilo": por_hilo.get(str(r["gmail_thread_id"]), []),
            "clasificacion": r["clasificacion"] or "necesita_revision",
            "confianza": r["confianza"] or 0,
        }
        for r in filas
    ]


def busqueda(c: Conn, r: Row) -> Row:
    return busquedas(c, [r])[0]


def busquedas(c: Conn, filas: list[Row]) -> list[Row]:
    ids = _ids(filas)
    resultados = (
        todos(
            c,
            "SELECT * FROM resultados_descubrimiento WHERE busqueda_id=ANY(%s::uuid[]) ORDER BY creado_en",
            (ids,),
        )
        if ids
        else []
    )
    rids = _ids(resultados)
    personas: dict[str, list[Row]] = defaultdict(list)
    if rids:
        for p in todos(
            c,
            "SELECT resultado_id,nombre,cargo,correo,requiere_base_legal FROM personas_descubiertas WHERE resultado_id=ANY(%s::uuid[])",
            (rids,),
        ):
            personas[str(p.pop("resultado_id"))].append(p)
    ev = evidencias_de(c, "resultado_id", rids)
    por_busqueda: dict[str, list[Row]] = defaultdict(list)
    for x in resultados:
        por_busqueda[str(x["busqueda_id"])].append(
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
                "personas": personas.get(str(x["id"]), []),
                "evidencias": ev.get(str(x["id"]), []),
                "posible_duplicado_de": x["posible_duplicado_de"],
                "estado": x["estado"],
            }
        )
    de_meta = (
        {
            t["b"]
            for t in todos(
                c,
                "SELECT datos->>'busquedaId' AS b FROM tareas WHERE tipo='descubrir_empresas' "
                "AND datos->>'meta'='true' AND datos->>'busquedaId'=ANY(%s)",
                ([str(i) for i in ids],),
            )
        }
        if ids
        else set()
    )
    return [
        {
            **r,
            "criterios": {k: r[k] for k in ("consulta", "municipios", "sectores", "tamano")},
            "resultados": por_busqueda.get(str(r["id"]), []),
            # Solo presente en las búsquedas que lanzó la meta diaria.
            **({"meta": True} if str(r["id"]) in de_meta else {}),
        }
        for r in filas
    ]


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


# Filtros de los listados (compartidos por lista() y total()).
FILTROS = {
    "empresas": " WHERE fusionada_en_id IS NULL",
    # Los contactos eliminados quedan como filas anónimas solo para el historial.
    "contactos": " WHERE anonimizado_en IS NULL",
    # Un par deja de tener sentido si una de las dos fichas ya se fusionó.
    "duplicados": """ d WHERE d.estado='pendiente'
        AND EXISTS (SELECT 1 FROM empresas a WHERE a.id=d.principal_id AND a.fusionada_en_id IS NULL)
        AND EXISTS (SELECT 1 FROM empresas b WHERE b.id=d.duplicado_id AND b.fusionada_en_id IS NULL)""",
}


def armar(c: Conn, recurso: str, r: Row) -> Row:
    return armar_lista(c, recurso, [r], detalle=True)[0]


def armar_lista(c: Conn, recurso: str, filas: list[Row], detalle: bool = False) -> list[Row]:
    if recurso == "empresas":
        return empresas(c, filas, con_evidencias=detalle)
    if recurso == "contactos":
        return contactos(c, filas, con_evidencias=detalle)
    if recurso == "segmentos":
        base = base_segmentos(c) if filas else None
        return [segmento(c, r, base) for r in filas]
    if recurso == "secuencias":
        return secuencias(c, filas)
    if recurso == "campanas":
        return campanas(c, filas)
    if recurso == "propuestas":
        return propuestas(c, filas)
    if recurso == "respuestas":
        return respuestas(c, filas)
    if recurso == "busquedas":
        return busquedas(c, filas)
    if recurso == "integraciones":
        from app.config import config

        # La integración «claude» controla al proveedor de agentes activo; con
        # Gemini configurado, el nombre lo dice para no confundir al administrador.
        gemini = config().agente_proveedor == "gemini"
        return [
            {
                **{k: r[k] for k in ("id", "nombre", "estado", "detalle", "ultima_sincronizacion")},
                **(
                    {"nombre": "API de Gemini (agentes de IA)"}
                    if gemini and r["id"] == "claude"
                    else {}
                ),
            }
            for r in filas
        ]
    if recurso == "auditoria":
        return [{**r, "id": str(r["id"])} for r in filas]
    if recurso == "oportunidades":
        return [{**r, "creada_en": r["creado_en"]} for r in filas]
    if recurso == "supresiones":
        sync = (
            {
                str(x["supresion_id"]): x
                for x in todos(
                    c,
                    "SELECT supresion_id,estado,intentos,ultimo_error,sincronizada_en "
                    "FROM sincronizacion_bajas WHERE supresion_id=ANY(%s::uuid[])",
                    ([str(r["id"]) for r in filas],),
                )
            }
            if filas
            else {}
        )
        out_s = []
        for r in filas:
            x = sync.get(str(r["id"]))
            out_s.append(
                {
                    **r,
                    "fecha": r["creado_en"],
                    # La baja ya es efectiva; esto solo dice si la copia secundaria existe.
                    "sincronizacion": (
                        {
                            "estado": x["estado"],
                            "intentos": x["intentos"],
                            "ultimoError": x["ultimo_error"],
                            "sincronizadaEn": x["sincronizada_en"],
                        }
                        if x
                        else None
                    ),
                }
            )
        return out_s
    return filas


def lista(
    c: Conn,
    recurso: str,
    ident: str | None = None,
    pagina: tuple[int, int] | None = None,
) -> Any:
    """Ficha (con ident) o listado. «pagina» = (límite, desplazamiento) opcional."""
    tabla = TABLAS[recurso]
    if ident:
        r = obtener(c, tabla, ident)
        if recurso == "empresas" and r["fusionada_en_id"]:
            raise HTTPException(404, "Empresa no encontrada")
        return armar(c, recurso, r)
    where = FILTROS.get(recurso, "")
    order = {
        "auditoria": " ORDER BY fecha DESC LIMIT 500",
        "empresas": " ORDER BY razon_social",
        "contactos": " ORDER BY creado_en DESC",
        "propuestas": " ORDER BY (estado='pendiente') DESC,creado_en DESC",
        "respuestas": " ORDER BY (estado='pendiente') DESC,recibida_en DESC",
        "campanas": " ORDER BY creado_en DESC",
        "segmentos": " ORDER BY creado_en DESC",
        "secuencias": " ORDER BY creado_en DESC",
        "oportunidades": " ORDER BY creado_en DESC",
        "supresiones": " ORDER BY creado_en DESC",
        "duplicados": " ORDER BY d.detectado_en DESC",
        "integraciones": " ORDER BY id",
    }.get(recurso, "")
    if pagina:
        limite, desplazamiento = pagina
        # Desempate por id: con claves repetidas (misma fecha o nombre) cada página
        # sería inestable y una fila podría repetirse u omitirse entre páginas.
        clave = "d.id" if recurso == "duplicados" else "id"
        if not order:
            order = f" ORDER BY {clave}"
        elif not order.endswith(f" {clave}"):
            order = f"{order}, {clave}"
        return armar_lista(
            c,
            recurso,
            todos(
                c,
                f"SELECT * FROM {tabla}{where}{order} LIMIT %s OFFSET %s",
                (limite, desplazamiento),
            ),
        )
    return armar_lista(c, recurso, todos(c, f"SELECT * FROM {tabla}{where}{order}"))


def total(c: Conn, recurso: str) -> int:
    """Total del listado con los mismos filtros que lista()."""
    tabla = TABLAS[recurso]
    where = FILTROS.get(recurso, "")
    return int(todos(c, f"SELECT count(*) AS n FROM {tabla}{where}")[0]["n"])
