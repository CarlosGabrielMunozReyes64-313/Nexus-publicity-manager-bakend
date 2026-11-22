from typing import Any
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from app.agentes import lector_web
from app.agentes.ejecutor import Claude, ejecutar
from app.agentes.modelos import (
    CampanaPropuesta,
    Clasificacion,
    Descubrimiento,
    Enriquecimiento,
    Informe,
    SegmentoPropuesto,
)
from app.api import datos, lecturas
from app.api.campanas import nueva_propuesta, versionar
from app.config import config
from app.db import Row, actualizar, insertar, obtener, todos, transaccion, uno
from app.dominio.reglas import BOGOTA, reglas_validas, validar_secuencia
from app.esquemas.serializar import serializar


def fuentes(datos_extraidos: list[Any], dominio: str | None = None) -> list[Row]:
    result = []
    paginas: dict[str, str | None] = {}
    for d in datos_extraidos:
        urlhost = (urlsplit(d.url).hostname or "").removeprefix("www.")
        if dominio and urlhost != dominio.removeprefix("www."):
            continue
        if d.url not in paginas:
            try:
                paginas[d.url] = lector_web.normalizar(lector_web.leer(d.url))
            except (ValueError, OSError):
                paginas[d.url] = None
        comprobada = bool(d.cita.strip()) and lector_web.normalizar(d.cita) in (
            paginas[d.url] or ""
        )
        result.append(
            {
                "campo": d.campo,
                "valor": d.valor,
                "fuente": "agente",
                "url": d.url,
                "cita": d.cita,
                "estado": "verificado" if comprobada else "inferido",
                "cita_verificada_en": datos.ahora() if comprobada else None,
            }
        )
    return result


def _correr(tipo: str, payload: Row, tarea_id: str, proveedor: Claude | None = None) -> None:
    with transaccion() as c:
        task = obtener(c, "tareas", tarea_id)
        if task["datos"].get("_aplicada"):
            return
        marca = uno(c, "SELECT valor FROM configuracion WHERE clave='marca'")["valor"]
        if tipo == "enriquecer_empresa":
            e = obtener(c, "empresas", payload["empresaId"])
            if not e["sitio_web"]:
                return
            entrada = {"empresa": serializar(e), "paginas": []}
        elif tipo == "descubrir_empresas":
            busqueda = obtener(c, "busquedas_descubrimiento", payload["busquedaId"])
            actualizar(
                c,
                "busquedas_descubrimiento",
                str(busqueda["id"]),
                {"estado": "ejecutando", "progreso": 10},
            )
            entrada = serializar(busqueda)
        elif tipo == "clasificar_respuesta":
            respuesta = obtener(c, "respuestas", payload["respuestaId"])
            entrada = {"texto": respuesta["texto"]}
        elif tipo in ("proponer_campana", "revisar_campana"):
            if tipo == "revisar_campana":
                prop = obtener(c, "propuestas", payload["propuestaId"])
                cam = obtener(c, "campanas", str(prop["objetivo_campana_id"]))
                seg = obtener(c, "segmentos", str(cam["segmento_id"]))
                comentario = uno(
                    c,
                    "SELECT comentario FROM propuesta_versiones WHERE propuesta_id=%s AND version=%s",
                    (prop["id"], prop["version_actual"]),
                )["comentario"]
            else:
                seg = obtener(c, "segmentos", payload["segmentoId"])
                comentario = None
            if seg["estado"] != "aprobado":
                raise ValueError("El segmento requiere aprobación")
            contactos = lecturas.datos_segmento(c, seg["reglas"])
            elegibles = [
                ct
                for ct in contactos
                if uno(c, "SELECT motivo_no_elegible(%s) AS motivo", (ct["id"],))["motivo"] is None
            ]
            if not elegibles:
                raise ValueError("No hay contactos elegibles con base legal aprobada")
            entrada = {
                "segmento": serializar(seg),
                "marca": marca,
                "elegibles": len(elegibles),
                "comentario": comentario,
            }
        elif tipo == "proponer_segmentos":
            entrada = {
                "empresas": serializar(
                    todos(
                        c,
                        "SELECT sector,municipio,tamano FROM empresas WHERE fusionada_en_id IS NULL",
                    )
                ),
                "clienteIdeal": uno(
                    c, "SELECT valor FROM configuracion WHERE clave='cliente_ideal'"
                )["valor"],
            }
        elif tipo == "informe_semanal":
            met = todos(c, "SELECT * FROM v_metricas_campana")
            if sum(x["enviados"] for x in met) < 30:
                insertar(
                    c,
                    "recomendaciones",
                    {
                        "texto": "Muestra insuficiente: menos de 30 envíos. No se recomiendan cambios.",
                        "sustento": Jsonb({"enviados": sum(x["enviados"] for x in met)}),
                    },
                )
                c.execute(
                    "UPDATE tareas SET datos=datos||'{\"_aplicada\":true}'::jsonb WHERE id=%s",
                    (tarea_id,),
                )
                return
            entrada = {"metricas": serializar(met)}
        else:
            raise ValueError("Tipo de agente desconocido")
    ev: list[Row] = []
    result: Any
    if tipo == "enriquecer_empresa":
        entrada["paginas"] = lector_web.leer_empresa(e["sitio_web"])
        result = ejecutar("investigador", entrada, Enriquecimiento, proveedor)
        ev = fuentes(result.datos, e["dominio"] or urlsplit(e["sitio_web"]).hostname)
    elif tipo == "descubrir_empresas":
        from app.agentes import buscador

        if buscador.usar_tavily():
            paginas, hechas = buscador.candidatos(entrada)
            with transaccion("agente:investigador", accion="buscar_tavily") as c:
                actualizar(c, "busquedas_descubrimiento", str(busqueda["id"]), {"progreso": 50})
                precio = config().tavily_costo_credito_usd
                if precio > 0 and hechas:
                    insertar(
                        c,
                        "costos",
                        {
                            "fecha": datos.ahora().astimezone(BOGOTA).date(),
                            "categoria": "otro",
                            "concepto": f"Tavily: {hechas} búsquedas",
                            "monto": round(precio * hechas, 2),
                            "moneda": "USD",
                        },
                    )
            # Sin páginas no hay nada que extraer: la búsqueda termina sin resultados y
            # no se gasta el modelo.
            result = (
                ejecutar(
                    "investigador",
                    {"criterios": entrada, "paginas": paginas},
                    buscador.DescubrimientoEnPaginas,
                    proveedor,
                )
                if paginas
                else buscador.DescubrimientoEnPaginas(empresas=[])
            )
        else:
            result = ejecutar("investigador", entrada, Descubrimiento, proveedor)
    elif tipo == "clasificar_respuesta":
        result = ejecutar("analista", entrada, Clasificacion, proveedor, ligero=True)
    elif tipo in ("proponer_campana", "revisar_campana"):
        result = ejecutar("estratega_redactor", entrada, CampanaPropuesta, proveedor)
        errors = validar_secuencia(
            result.secuencia.model_dump(by_alias=True, exclude_none=True), marca["prohibidas"]
        )
        if errors:
            result = ejecutar(
                "estratega_redactor",
                {"entrada": entrada, "corregir": errors},
                CampanaPropuesta,
                proveedor,
            )
            errors = validar_secuencia(
                result.secuencia.model_dump(by_alias=True, exclude_none=True), marca["prohibidas"]
            )
        if errors:
            raise ValueError("Secuencia no válida; requiere revisión: " + ". ".join(errors))
    elif tipo == "proponer_segmentos":
        result = ejecutar("segmentador", entrada, SegmentoPropuesto, proveedor)
        reglas_validas(result.reglas)
    else:
        result = ejecutar("analista", entrada, Informe, proveedor)
    agente = (
        "investigador"
        if tipo in ("enriquecer_empresa", "descubrir_empresas")
        else "estratega_redactor"
        if tipo in ("proponer_campana", "revisar_campana")
        else "segmentador"
        if tipo == "proponer_segmentos"
        else "analista"
    )
    with transaccion("agente:" + agente, accion=tipo) as c:
        task = obtener(c, "tareas", tarea_id, True)
        if task["datos"].get("_aplicada"):
            return
        if tipo == "enriquecer_empresa":
            # All changes remain proposals; verified evidence never overwrites existing data autonomously.
            fields = {x["campo"]: x["valor"] for x in ev if x["valor"] is not None}
            prop = nueva_propuesta(
                c,
                "investigador",
                "enriquecimiento",
                result.resumen,
                {"campos": fields},
                {"objetivo_empresa_id": e["id"]},
            )
            for item in ev:
                insertar(c, "evidencias", {"propuesta_id": prop["id"], **item})
        elif tipo == "descubrir_empresas":
            from app.agentes.descubrimiento import corroborados, importar

            automatico = bool(payload.get("autoImportar"))
            limite = (
                min(5, max(1, int(payload.get("limite", 5))))
                if automatico
                else len(result.empresas)
            )
            for x in result.empresas[:limite]:
                host = urlsplit(x.sitioWeb).hostname
                if not host:
                    continue
                comprobadas = fuentes(x.datos, host)
                if not any(
                    d["estado"] == "verificado"
                    and d["campo"] == "razonSocial"
                    and d["valor"] == x.razonSocial
                    for d in comprobadas
                ):
                    continue
                duplicate = uno(
                    c,
                    "SELECT id FROM empresas WHERE dominio=%s AND fusionada_en_id IS NULL",
                    (host.removeprefix("www."),),
                    False,
                )
                verified = {
                    d["campo"]: d["valor"] for d in comprobadas if d["estado"] == "verificado"
                }
                if automatico:
                    verified = corroborados(comprobadas)
                    if str(verified.get("pais", "")).casefold() != "colombia":
                        continue
                r = insertar(
                    c,
                    "resultados_descubrimiento",
                    {
                        "busqueda_id": busqueda["id"],
                        "razon_social": x.razonSocial,
                        "sitio_web": x.sitioWeb,
                        "dominio": host.removeprefix("www."),
                        "sector": verified.get("sector"),
                        "municipio": verified.get("municipio"),
                        "correo_corporativo": verified.get("correoCorporativo"),
                        "posible_duplicado_de": duplicate.get("id"),
                    },
                )
                for item in comprobadas:
                    insertar(c, "evidencias", {"resultado_id": r["id"], **item})
                if automatico and not duplicate:
                    importar(c, r, comprobadas)
            actualizar(
                c,
                "busquedas_descubrimiento",
                str(busqueda["id"]),
                {
                    "estado": "terminada",
                    "progreso": 100,
                    "terminada_en": datos.ahora(),
                    "error": None,
                },
            )
        elif tipo == "clasificar_respuesta":
            current = obtener(c, "respuestas", str(respuesta["id"]), True)
            if not current["clasificacion_confirmada"]:
                actualizar(
                    c,
                    "respuestas",
                    str(respuesta["id"]),
                    {
                        "clasificacion": result.clasificacion,
                        "confianza": result.confianza,
                        "borrador": result.borrador,
                    },
                )
                if result.confianza >= 0.85 and result.clasificacion == "baja":
                    ct = obtener(c, "contactos", str(respuesta["contacto_id"]))
                    datos.suprimir(
                        c,
                        {"correo": ct["correo"], "motivo": "Solicitud de baja detectada"},
                        "respuesta",
                    )
                    actualizar(
                        c,
                        "respuestas",
                        str(respuesta["id"]),
                        {"estado": "atendida", "atendida_en": datos.ahora()},
                    )
        elif tipo in ("proponer_campana", "revisar_campana"):
            s = result.secuencia.model_dump(by_alias=True, exclude_none=True)
            if tipo == "revisar_campana":
                # Agent creates a proposal version only; never edits an approved campaign.
                prop = obtener(c, "propuestas", payload["propuestaId"], True)
                versionar(c, prop, s, result.resumen)
            else:
                seq = insertar(
                    c,
                    "secuencias",
                    {
                        "nombre": s["nombre"],
                        "objetivo": s["objetivo"],
                        "llamada_accion": s["llamadaAccion"],
                        "metricas_exito": s["metricasExito"],
                        "ventana_dias": s["ventanaEnvio"]["dias"],
                        "ventana_desde": s["ventanaEnvio"]["desde"],
                        "ventana_hasta": s["ventanaEnvio"]["hasta"],
                    },
                )
                for i, paso in enumerate(s["pasos"], 1):
                    insertar(
                        c,
                        "pasos",
                        {
                            "secuencia_id": seq["id"],
                            "orden": i,
                            "asunto": paso.get("asunto"),
                            "cuerpo": paso["cuerpo"],
                            "espera_dias_habiles": paso["esperaDiasHabiles"],
                            "mismo_hilo": paso["mismoHilo"],
                            "variables": Jsonb(paso["variables"]),
                        },
                    )
                cam = insertar(
                    c,
                    "campanas",
                    {
                        "nombre": result.nombre,
                        "segmento_id": seg["id"],
                        "secuencia_id": seq["id"],
                        "estado": "en_revision",
                    },
                )
                nueva_propuesta(
                    c,
                    "estratega_redactor",
                    "campana",
                    result.resumen,
                    serializar(lecturas.secuencia(c, obtener(c, "secuencias", str(seq["id"])))),
                    {"objetivo_campana_id": cam["id"]},
                )
        elif tipo == "proponer_segmentos":
            seg = insertar(
                c,
                "segmentos",
                {
                    "nombre": result.nombre,
                    "reglas": Jsonb(result.reglas),
                    "justificacion": result.justificacion,
                    "propuesto_por": "agente",
                },
            )
            nueva_propuesta(
                c,
                "segmentador",
                "segmento",
                result.justificacion,
                result.model_dump(),
                {"objetivo_segmento_id": seg["id"]},
            )
        else:
            for rec in result.recomendaciones:
                insertar(
                    c, "recomendaciones", {"texto": rec.texto, "sustento": Jsonb(rec.sustento)}
                )
        c.execute(
            "UPDATE tareas SET datos=datos||'{\"_aplicada\":true}'::jsonb WHERE id=%s", (tarea_id,)
        )


def correr(tipo: str, payload: Row, tarea_id: str, proveedor: Claude | None = None) -> None:
    from app.agentes.ejecutor import TAREA_ACTUAL

    token = TAREA_ACTUAL.set(tarea_id)
    try:
        _correr(tipo, payload, tarea_id, proveedor)
    finally:
        TAREA_ACTUAL.reset(token)
