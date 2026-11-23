import re
from typing import Any
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from app.agentes import lector_web
from app.agentes.ejecutor import Claude, ejecutar
from app.agentes.modelos import (
    CampanaPropuesta,
    Clasificacion,
    CorreoReescrito,
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


VARIABLES_CONOCIDAS = {
    "nombre",
    "empresa",
    "senal",
    "cargo",
    "municipio",
    "enlace",
    "firma",
    "baja",
}


def problemas_de_correo(r: Any, lleva_asunto: bool, prohibidas: list[str]) -> list[str]:
    """Las reglas de un correo (las de validar_secuencia) para una sugerencia suelta."""
    errores = []
    cuerpo = r.cuerpo or ""
    if lleva_asunto and r.asunto is not None and not r.asunto.strip():
        errores.append("El asunto no puede quedar vacío")
    if r.asunto and len(r.asunto) > 200:
        errores.append("El asunto admite hasta 200 caracteres")
    for var in ("firma", "baja"):
        if "{{" + var + "}}" not in cuerpo:
            errores.append(f"Falta {{{{{var}}}}}")
    if not 30 <= len(cuerpo) <= 3000:
        errores.append("El cuerpo debe tener entre 30 y 3000 caracteres")
    texto = (cuerpo + " " + (r.asunto or "")).lower()
    errores.extend("Palabra prohibida: " + w for w in prohibidas if w and w.lower() in texto)
    desconocidas = sorted(
        set(re.findall(r"\{\{(\w+)\}\}", cuerpo + (r.asunto or ""))) - VARIABLES_CONOCIDAS
    )
    errores.extend(f"Variable desconocida: {{{{{v}}}}}" for v in desconocidas)
    if not r.porQue.strip():
        errores.append("Falta porQue")
    return errores


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
            from app.api import rutas_correo

            rechazada: Row | None = None
            if tipo == "revisar_campana":
                prop = obtener(c, "propuestas", payload["propuestaId"])
                cam = obtener(c, "campanas", str(prop["objetivo_campana_id"]))
                seg = obtener(c, "segmentos", str(cam["segmento_id"]))
                rechazada = uno(
                    c,
                    "SELECT contenido,comentario FROM propuesta_versiones "
                    "WHERE propuesta_id=%s AND version=%s",
                    (prop["id"], prop["version_actual"]),
                )
                origen = uno(
                    c,
                    "SELECT datos FROM tareas WHERE tipo='proponer_campana' "
                    "AND datos->>'propuestaId'=%s ORDER BY creada_en DESC LIMIT 1",
                    (str(prop["id"]),),
                    False,
                )
                solicitud = rutas_correo.solicitud_de((origen or {}).get("datos") or {})
            else:
                seg = obtener(c, "segmentos", payload["segmentoId"])
                solicitud = rutas_correo.solicitud_de(payload)
            if seg["estado"] != "aprobado":
                raise ValueError(rutas_correo.SEGMENTO_SIN_APROBAR)
            entrada = rutas_correo.entrada_agente(c, seg, solicitud)
            if not entrada["elegibles"]:
                raise ValueError(rutas_correo.SIN_ELEGIBLES)
            numero_correos = solicitud["numeroCorreos"] if tipo == "proponer_campana" else None
            if rechazada is not None:
                entrada["versionRechazada"] = rutas_correo.normalizar(rechazada["contenido"])
                entrada["notasVersionRechazada"] = (
                    rechazada["contenido"].get("notasAgente")
                    if isinstance(rechazada["contenido"], dict)
                    else None
                )
                entrada["comentario"] = payload.get("comentario") or rechazada["comentario"]
                entrada["comentariosAnteriores"] = [
                    x
                    for x in rutas_correo.comentarios_rechazo(c, str(prop["id"]))
                    if x != rechazada["comentario"]
                ]
        elif tipo == "reescribir_correo":
            from app.api import rutas_correo

            # Solo el texto del correo (con sus variables) y cifras agregadas del segmento:
            # ningún dato personal de los contactos.
            seg = obtener(c, "segmentos", payload["segmentoId"])
            contexto = payload.get("contexto") or {}
            orden = int(payload["orden"])
            original = payload["original"]
            entrada = rutas_correo.entrada_agente(
                c,
                seg,
                {
                    "objetivo": contexto.get("objetivo") or rutas_correo.OBJETIVO_POR_DEFECTO,
                    "numeroCorreos": len(contexto.get("pasos") or []) or 1,
                },
            )
            entrada["secuencia"] = {
                "objetivo": contexto.get("objetivo"),
                "llamadaAccion": contexto.get("llamadaAccion"),
                "correos": [
                    {k: p.get(k) for k in ("orden", "asunto", "cuerpo", "mismoHilo") if p.get(k)}
                    for p in contexto.get("pasos") or []
                ],
            }
            entrada["reescribirCorreo"] = {
                "orden": orden,
                "llevaAsunto": bool(original.get("llevaAsunto")),
                "asunto": original.get("asunto"),
                "cuerpo": original["cuerpo"],
                "instruccion": payload["instruccion"],
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

        def problemas(r: CampanaPropuesta) -> list[str]:
            s = r.secuencia.model_dump(by_alias=True, exclude_none=True)
            errores = validar_secuencia(s, marca["prohibidas"])
            if numero_correos and len(s["pasos"]) != numero_correos:
                errores.append(
                    f"Se pidieron {numero_correos} correos y la secuencia tiene {len(s['pasos'])}"
                )
            return errores

        result = ejecutar("estratega_redactor", entrada, CampanaPropuesta, proveedor)
        errors = problemas(result)
        # Hasta dos correcciones con la lista de errores; si sigue sin cumplir, la tarea
        # falla sin reintentos (repetir cuesta y no lo corrige) y el motivo queda visible.
        for _ in range(2):
            if not errors:
                break
            result = ejecutar(
                "estratega_redactor",
                {
                    "entrada": entrada,
                    "rutaAnterior": result.secuencia.model_dump(by_alias=True, exclude_none=True),
                    "corregir": errors,
                },
                CampanaPropuesta,
                proveedor,
            )
            errors = problemas(result)
        if errors:
            raise ValueError("La secuencia propuesta no cumple: " + ". ".join(errors))
    elif tipo == "reescribir_correo":
        lleva_asunto = bool(payload["original"].get("llevaAsunto"))

        def problemas_correo(r: CorreoReescrito) -> list[str]:
            return problemas_de_correo(r, lleva_asunto, marca["prohibidas"])

        result = ejecutar("estratega_redactor", entrada, CorreoReescrito, proveedor)
        errors = problemas_correo(result)
        for _ in range(2):
            if not errors:
                break
            result = ejecutar(
                "estratega_redactor",
                {"entrada": entrada, "correoAnterior": result.model_dump(), "corregir": errors},
                CorreoReescrito,
                proveedor,
            )
            errors = problemas_correo(result)
        if errors:
            raise ValueError("La sugerencia no cumple: " + ". ".join(errors))
    elif tipo == "proponer_segmentos":
        result = ejecutar("segmentador", entrada, SegmentoPropuesto, proveedor)
        reglas_validas(result.reglas)
    else:
        result = ejecutar("analista", entrada, Informe, proveedor)
    agente = (
        "investigador"
        if tipo in ("enriquecer_empresa", "descubrir_empresas")
        else "estratega_redactor"
        if tipo in ("proponer_campana", "revisar_campana", "reescribir_correo")
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
            # El agente siempre escribe texto simple; el formato HTML lo elige el administrador.
            for paso in s["pasos"]:
                paso["formato"] = "texto"
                paso.pop("cuerpoHtml", None)
            notas = result.notas.model_dump()
            if tipo == "revisar_campana":
                # El agente solo crea una versión (un borrador); nunca edita lo aprobado.
                prop = obtener(c, "propuestas", payload["propuestaId"], True)
                cam_rev = obtener(c, "campanas", str(prop["objetivo_campana_id"]))
                versionar(
                    c,
                    prop,
                    {**s, "id": str(cam_rev["secuencia_id"]), "notasAgente": notas},
                    result.resumen,
                    autor="agente",
                )
                c.execute(
                    "UPDATE tareas SET datos=datos||jsonb_build_object('secuenciaId',%s::text) "
                    "WHERE id=%s",
                    (str(cam_rev["secuencia_id"]), tarea_id),
                )
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
                creada = nueva_propuesta(
                    c,
                    "estratega_redactor",
                    "campana",
                    result.resumen,
                    {
                        **serializar(
                            lecturas.secuencia(c, obtener(c, "secuencias", str(seq["id"])))
                        ),
                        "notasAgente": notas,
                    },
                    {"objetivo_campana_id": cam["id"]},
                )
                # Así la lista de solicitudes enlaza con la secuencia creada.
                c.execute(
                    "UPDATE tareas SET datos=datos||jsonb_build_object('propuestaId',%s::text,"
                    "'secuenciaId',%s::text) WHERE id=%s",
                    (str(creada["id"]), str(seq["id"]), tarea_id),
                )
        elif tipo == "reescribir_correo":
            # Solo una sugerencia: el administrador decide si la usa en su borrador.
            sugerencia = {"cuerpo": result.cuerpo.strip(), "porQue": result.porQue.strip()}
            if lleva_asunto:
                sugerencia["asunto"] = (result.asunto or "").strip() or payload["original"].get(
                    "asunto"
                )
            c.execute(
                "UPDATE tareas SET datos=datos||jsonb_build_object('resultado',%s::jsonb) "
                "WHERE id=%s",
                (Jsonb(sugerencia), tarea_id),
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
