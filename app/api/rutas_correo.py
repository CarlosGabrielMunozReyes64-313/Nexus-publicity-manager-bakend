"""Rutas de correo: propuestas de tipo «campana» del agente estratega_redactor.

Una ruta es una secuencia de correos para un segmento aprobado. Aquí viven la
solicitud al agente, el seguimiento de esas solicitudes, la revisión (la misma lógica
que usa aprobar), la edición con versiones, la vista previa de una versión y la
restauración. El agente solo propone: nada de este módulo aprueba, activa ni envía.
"""

import re
from collections import Counter
from typing import Any

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.api import datos, lecturas
from app.config import config
from app.db import Conn, Row, auditar, encolar, obtener, todos, uno
from app.dominio.reglas import VentanaIncompatible, siguiente_ventana, validar_secuencia
from app.esquemas.entradas import Secuencia
from app.esquemas.serializar import serializar

OBJETIVO_POR_DEFECTO = "Que completen el diagnóstico RSE Express"
TIPOS_SOLICITUD = ("proponer_campana", "revisar_campana")
EN_CURSO = ("pendiente", "en_proceso")
ESTADOS_SOLICITUD = {
    "pendiente": "en_cola",
    "en_proceso": "trabajando",
    "completada": "lista",
    "fallida": "fallida",
    "cancelada": "fallida",
}
# Variables de datos (no de sistema) y el campo de evidencia que las respalda.
VARIABLES_DATOS = ("nombre", "empresa", "senal", "cargo", "municipio")
SIN_ELEGIBLES = "El segmento no tiene contactos elegibles con base legal aprobada"
SEGMENTO_SIN_APROBAR = "El segmento requiere aprobación"
SECUENCIA_ARCHIVADA = "La secuencia está archivada"
VENTANA_GLOBAL = (
    "La ventana de envío de la secuencia no coincide con el horario global "
    "(Configuración → Límites y horarios)"
)
MAX_PALABRAS = 120
MAX_ASUNTO = 60
MAX_DIAS_HABILES = 15


# ---------------------------------------------------------------------- utilidades


def normalizar(contenido: Any) -> dict[str, Any] | None:
    """Secuencia normalizada (la base de toda comparación); None si no es una ruta."""
    if not isinstance(contenido, dict) or "pasos" not in contenido:
        return None
    try:
        return Secuencia.model_validate(contenido).model_dump(by_alias=True, exclude_none=True)
    except ValidationError:
        return None


def palabras(cuerpo: str) -> int:
    """Palabras del cuerpo sin contar {{firma}} ni {{baja}} (texto fijo del sistema)."""
    return len(re.sub(r"\{\{(firma|baja)\}\}", " ", cuerpo).split())


def elegibles_ids(c: Conn, reglas: list[dict[str, str]]) -> list[str]:
    """Contactos del segmento que hoy pueden recibir correos (lo decide la base)."""
    ids = lecturas.ids_segmento(c, reglas)
    if not ids:
        return []
    return [
        str(r["id"])
        for r in todos(
            c,
            "SELECT x.id FROM unnest(%s::uuid[]) AS x(id) WHERE motivo_no_elegible(x.id) IS NULL",
            (ids,),
        )
    ]


def datos_verificados(c: Conn, ids: list[str]) -> dict[str, int]:
    """Por variable, cuántos contactos tienen el dato con evidencia verificada.

    Sigue la misma regla que render(): el valor debe coincidir con una evidencia
    «verificado» del contacto o de su empresa. No devuelve datos, solo conteos.
    """
    if not ids:
        return dict.fromkeys(VARIABLES_DATOS, 0)
    r = uno(
        c,
        """
        WITH ct AS (
          SELECT c.id, c.empresa_id, c.nombre, c.cargo, e.razon_social, e.municipio,
                 (SELECT s.descripcion FROM senales s WHERE s.empresa_id = c.empresa_id
                  ORDER BY s.detectada_en LIMIT 1) AS senal
          FROM contactos c JOIN empresas e ON e.id = c.empresa_id
          WHERE c.id = ANY(%s::uuid[])
        ), ev AS (
          SELECT ct.id AS contacto, v.campo, v.valor
          FROM ct JOIN evidencias v
            ON (v.contacto_id = ct.id OR v.empresa_id = ct.empresa_id) AND v.estado = 'verificado'
        )
        SELECT
          count(*) FILTER (WHERE EXISTS (SELECT 1 FROM ev WHERE ev.contacto=ct.id
            AND ev.campo='nombre' AND ev.valor=ct.nombre)) AS nombre,
          count(*) FILTER (WHERE EXISTS (SELECT 1 FROM ev WHERE ev.contacto=ct.id
            AND ev.campo='cargo' AND ev.valor=ct.cargo)) AS cargo,
          count(*) FILTER (WHERE EXISTS (SELECT 1 FROM ev WHERE ev.contacto=ct.id
            AND ev.campo='razonSocial' AND ev.valor=ct.razon_social)) AS empresa,
          count(*) FILTER (WHERE EXISTS (SELECT 1 FROM ev WHERE ev.contacto=ct.id
            AND ev.campo='municipio' AND ev.valor=ct.municipio)) AS municipio,
          count(*) FILTER (WHERE EXISTS (SELECT 1 FROM ev WHERE ev.contacto=ct.id
            AND ev.campo='senal' AND ev.valor=ct.senal)) AS senal
        FROM ct
        """,
        (ids,),
    )
    return {k: int(r[k]) for k in VARIABLES_DATOS}


def resumen_elegibles(c: Conn, ids: list[str]) -> dict[str, Any]:
    """Cifras agregadas de los elegibles para el agente (sin nombres ni correos)."""
    if not ids:
        return {"sectores": {}, "municipios": {}, "tamanos": {}, "senales": []}
    filas = todos(
        c,
        """SELECT e.sector, e.municipio, e.tamano FROM contactos c
           JOIN empresas e ON e.id=c.empresa_id WHERE c.id=ANY(%s::uuid[])""",
        (ids,),
    )
    senales = todos(
        c,
        """SELECT s.tipo, count(DISTINCT c.id) AS contactos FROM contactos c
           JOIN senales s ON s.empresa_id=c.empresa_id WHERE c.id=ANY(%s::uuid[])
           GROUP BY s.tipo ORDER BY 2 DESC, 1 LIMIT 5""",
        (ids,),
    )
    return {
        "sectores": dict(Counter(f["sector"] or "Sin dato" for f in filas).most_common(10)),
        "municipios": dict(Counter(f["municipio"] or "Sin dato" for f in filas).most_common(10)),
        "tamanos": dict(Counter(f["tamano"] or "desconocido" for f in filas).most_common()),
        "senales": [{"tipo": s["tipo"], "contactos": int(s["contactos"])} for s in senales],
    }


def reglas_sin_ids(reglas: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Las reglas por lista de contactos se resumen: el agente no recibe identificadores."""
    out: list[dict[str, Any]] = []
    for r in reglas:
        if r.get("campo") == "contactoIds":
            out.append(
                {"campo": "contactos_elegidos_a_mano", "cantidad": len(r["valor"].split(","))}
            )
        else:
            out.append(dict(r))
    return out


def entrada_agente(c: Conn, seg: Row, solicitud: dict[str, Any]) -> dict[str, Any]:
    """Entrada de proponer_campana: cifras y señales agregadas, nunca datos personales."""
    ids = elegibles_ids(c, seg["reglas"])
    marca = uno(c, "SELECT valor FROM configuracion WHERE clave='marca'")["valor"]
    ideal = uno(c, "SELECT valor FROM configuracion WHERE clave='cliente_ideal'")["valor"]
    limites = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
    return {
        "solicitud": solicitud,
        "segmento": {
            "nombre": seg["nombre"],
            "justificacion": seg.get("justificacion") or "",
            "version": seg["version"],
            "reglas": reglas_sin_ids(seg["reglas"]),
        },
        "elegibles": len(ids),
        "resumenElegibles": resumen_elegibles(c, ids),
        "datosVerificados": datos_verificados(c, ids),
        "marca": {k: marca.get(k) for k in ("nombre", "oferta", "tono", "firma", "prohibidas")},
        "clienteIdeal": ideal.get("criterios", []),
        "ventanaGlobal": limites.get("ventana"),
    }


def solicitud_de(datos_tarea: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "objetivo": datos_tarea.get("objetivo") or OBJETIVO_POR_DEFECTO,
        "numeroCorreos": int(datos_tarea.get("numeroCorreos") or 3),
    }
    if datos_tarea.get("notas"):
        out["notas"] = datos_tarea["notas"]
    return out


# ---------------------------------------------------------------------- solicitudes


def validar_solicitud(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(400, "Solicitud no válida")
    segmento = str(body.get("segmentoId") or "").strip()
    if not segmento:
        raise HTTPException(400, "Elija un segmento")
    objetivo = " ".join(str(body.get("objetivo") or "").split())
    if not 5 <= len(objetivo) <= 200:
        raise HTTPException(400, "El objetivo requiere entre 5 y 200 caracteres")
    numero = body.get("numeroCorreos", 3)
    if isinstance(numero, bool) or not isinstance(numero, int) or not 2 <= numero <= 5:
        raise HTTPException(400, "El número de correos debe ser un entero entre 2 y 5")
    notas = str(body.get("notas") or "").strip()
    if len(notas) > 500:
        raise HTTPException(400, "Las notas admiten hasta 500 caracteres")
    out: dict[str, Any] = {"segmentoId": segmento, "objetivo": objetivo, "numeroCorreos": numero}
    if notas:
        out["notas"] = notas
    return out


def encolar_ruta(c: Conn, datos_tarea: dict[str, Any]) -> str:
    """Valida en el momento y encola proponer_campana. Devuelve el id de la tarea."""
    from app.agentes.solicitudes import proveedor_configurado

    proveedor_configurado()
    seg = obtener(c, "segmentos", datos_tarea["segmentoId"])
    if seg["estado"] != "aprobado":
        raise HTTPException(409, SEGMENTO_SIN_APROBAR)
    if not elegibles_ids(c, seg["reglas"]):
        raise HTTPException(409, SIN_ELEGIBLES)
    if uno(
        c,
        "SELECT count(*) AS n FROM tareas WHERE tipo='proponer_campana' AND estado=ANY(%s) "
        "AND datos->>'segmentoId'=%s",
        (list(EN_CURSO), str(seg["id"])),
    )["n"]:
        raise HTTPException(409, "Ya hay una secuencia en preparación para este segmento")
    from uuid import uuid4

    clave = f"proponer_campana:{seg['id']}:{uuid4()}"
    encolar(c, "proponer_campana", {**datos_tarea, "segmentoId": str(seg["id"])}, clave, 6)
    tarea = uno(c, "SELECT id FROM tareas WHERE clave_unica=%s", (clave,))
    auditar(c, "pedir_ruta_correo", "segmentos", str(seg["id"]))
    return str(tarea["id"])


def pedir(c: Conn, body: Any) -> dict[str, Any]:
    encolar_ruta(c, validar_solicitud(body))
    return {"ok": True}


def solicitudes(c: Conn, dias: int = 30, tipos: tuple[str, ...] = TIPOS_SOLICITUD) -> list[Row]:
    filas = todos(
        c,
        """SELECT t.id,t.tipo,t.estado,t.datos,t.ultimo_error,t.creada_en,t.terminada_en,
                  coalesce(t.datos->>'segmentoId', cam.segmento_id::text) AS segmento_id,
                  coalesce(t.datos->>'secuenciaId', cam.secuencia_id::text) AS secuencia_id,
                  s.nombre AS segmento
           FROM tareas t
           LEFT JOIN propuestas p ON p.id::text = t.datos->>'propuestaId'
           LEFT JOIN campanas cam ON cam.id = p.objetivo_campana_id
           LEFT JOIN segmentos s ON s.id::text = coalesce(t.datos->>'segmentoId', cam.segmento_id::text)
           WHERE t.tipo = ANY(%s) AND t.creada_en > now() - make_interval(days => %s)
           ORDER BY t.creada_en DESC""",
        (list(tipos), dias),
    )
    out = []
    for f in filas:
        estado = ESTADOS_SOLICITUD.get(f["estado"], "en_cola")
        d = f["datos"] or {}
        error = None
        if estado == "fallida":
            error = (
                "Solicitud cancelada"
                if f["estado"] == "cancelada"
                else f["ultimo_error"] or "La solicitud no pudo completarse"
            )
        out.append(
            {
                "id": f["id"],
                "tipo": f["tipo"],
                "estado": estado,
                "segmento_id": f["segmento_id"],
                "segmento": f["segmento"],
                "propuesta_id": d.get("propuestaId"),
                "secuencia_id": f["secuencia_id"],
                "objetivo": d.get("objetivo")
                or (OBJETIVO_POR_DEFECTO if f["tipo"] == "proponer_campana" else None),
                "numero_correos": d.get("numeroCorreos"),
                "creada_en": f["creada_en"],
                "terminada_en": f["terminada_en"],
                "error": error,
            }
        )
    return out


def reintentar(c: Conn, tarea_id: str, tipos: tuple[str, ...] = TIPOS_SOLICITUD) -> dict[str, Any]:
    t = obtener(c, "tareas", tarea_id, True)
    if t["tipo"] not in tipos:
        raise HTTPException(404, "Solicitud no encontrada")
    if ESTADOS_SOLICITUD.get(t["estado"]) != "fallida":
        raise HTTPException(409, "Solo se puede reintentar una solicitud fallida")
    d = {k: v for k, v in (t["datos"] or {}).items() if not k.startswith("_")}
    if t["tipo"] == "proponer_campana":
        d.pop("propuestaId", None)
        d.pop("secuenciaId", None)
        encolar_ruta(c, d)
    elif t["tipo"] == "reescribir_correo":
        from uuid import uuid4

        from app.agentes.solicitudes import proveedor_configurado

        proveedor_configurado()
        d.pop("resultado", None)
        d.pop("descartada", None)
        encolar(c, "reescribir_correo", d, f"reescribir_correo:{d.get('secuenciaId')}:{uuid4()}", 5)
    else:
        if uno(
            c,
            "SELECT count(*) AS n FROM tareas WHERE tipo='revisar_campana' AND estado=ANY(%s) "
            "AND datos->>'propuestaId'=%s",
            (list(EN_CURSO), d.get("propuestaId")),
        )["n"]:
            raise HTTPException(409, "Ya hay una versión en preparación para esta ruta")
        from uuid import uuid4

        from app.agentes.solicitudes import proveedor_configurado

        proveedor_configurado()
        encolar(c, "revisar_campana", d, f"revisar_campana:{d.get('propuestaId')}:{uuid4()}", 6)
    auditar(c, "reintentar_solicitud_ruta", "tareas", tarea_id)
    return {"ok": True}


def segmentos_para_ruta(c: Conn) -> list[Row]:
    """Segmentos aprobados con su número de elegibles (para el diálogo de solicitud)."""
    base = lecturas.base_segmentos(c)
    out = []
    for s in todos(
        c, "SELECT id,nombre,reglas,version FROM segmentos WHERE estado='aprobado' ORDER BY nombre"
    ):
        ids = lecturas.ids_segmento(c, s["reglas"], base)
        n = 0
        if ids:
            n = int(
                uno(
                    c,
                    "SELECT count(*) AS n FROM unnest(%s::uuid[]) AS x(id) "
                    "WHERE motivo_no_elegible(x.id) IS NULL",
                    (ids,),
                )["n"]
            )
        out.append({"id": s["id"], "nombre": s["nombre"], "version": s["version"], "elegibles": n})
    return out


# ---------------------------------------------------------------------- revisión


def _segmento_de(c: Conn, p: Row) -> tuple[Row, Row]:
    cam = obtener(c, "campanas", str(p["objetivo_campana_id"]))
    return cam, obtener(c, "segmentos", str(cam["segmento_id"]))


def elegibles_de(c: Conn, p: Row, limite: int = 50) -> list[Row]:
    """Contactos elegibles del segmento de la ruta, para elegir uno en la vista previa."""
    if p["tipo"] != "campana":
        raise HTTPException(400, "Solo las rutas de correo tienen contactos elegibles")
    _, seg = _segmento_de(c, p)
    ids = elegibles_ids(c, seg["reglas"])[:limite]
    if not ids:
        return []
    return todos(
        c,
        """SELECT c.id, c.nombre, e.razon_social AS empresa FROM contactos c
           JOIN empresas e ON e.id=c.empresa_id WHERE c.id=ANY(%s::uuid[])
           ORDER BY c.nombre, c.id""",
        (ids,),
    )


def advertencias_de(s: dict[str, Any]) -> list[str]:
    avisos = []
    for i, paso in enumerate(s["pasos"], 1):
        n = palabras(paso["cuerpo"])
        if n > MAX_PALABRAS:
            avisos.append(f"Correo {i}: {n} palabras; se recomienda no pasar de {MAX_PALABRAS}")
        asunto = paso.get("asunto") or ""
        if len(asunto) > MAX_ASUNTO:
            avisos.append(
                f"Correo {i}: asunto de {len(asunto)} caracteres; se recomienda menos de {MAX_ASUNTO}"
            )
        if re.search(r"[A-ZÁÉÍÓÚÑ]{6,}", asunto) or re.search(
            r"\b[A-ZÁÉÍÓÚÑ]{2,}\s+[A-ZÁÉÍÓÚÑ]{2,}\b", asunto
        ):
            avisos.append(f"Correo {i}: el asunto usa mayúsculas sostenidas")
        if re.search(r"[!?¡¿]{2,}", asunto):
            avisos.append(f"Correo {i}: el asunto repite signos de puntuación")
        if paso["cuerpo"].count("{{enlace}}") > 1:
            avisos.append(
                f"Correo {i}: tiene más de un {{{{enlace}}}}; use una sola llamada a la acción"
            )
    dias = sum(p["esperaDiasHabiles"] for p in s["pasos"])
    if dias > MAX_DIAS_HABILES:
        avisos.append(
            f"La ruta dura {dias} días hábiles; se recomienda no pasar de {MAX_DIAS_HABILES}"
        )
    return avisos


def revision(c: Conn, p: Row, version: Row | None = None) -> dict[str, Any]:
    """Lista de verificación de la versión actual. aprobar() usa exactamente esta lógica."""
    v = version or uno(
        c,
        "SELECT * FROM propuesta_versiones WHERE propuesta_id=%s AND version=%s",
        (p["id"], p["version_actual"]),
    )
    bloqueos: list[str] = []
    errores: list[str] = []
    if p["estado"] == "aprobada":
        bloqueos.append("Propuesta ya aprobada")
    if v["decision"]:
        bloqueos.append("Esta versión ya fue decidida; cree una versión nueva")
    if p["tipo"] == "correo_inferido":
        bloqueos.append("Un correo inferido requiere verificación; no se aprueba para envíos")
    if p["tipo"] != "campana":
        return {
            "puedeAprobar": not bloqueos,
            "bloqueos": bloqueos,
            "errores": [],
            "advertencias": [],
            "elegibles": 0,
        }
    cam, seg = _segmento_de(c, p)
    marca = uno(c, "SELECT valor FROM configuracion WHERE clave='marca'")["valor"]
    limites = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
    s = normalizar(v["contenido"])
    advertencias: list[str] = []
    ventana_valida = False
    duracion = 0
    if s is None:
        errores.append("El contenido no es una ruta de correos válida")
    else:
        errores.extend(validar_secuencia(s, marca["prohibidas"]))
        try:
            siguiente_ventana(datos.ahora(), s["ventanaEnvio"], limites["ventana"])
            ventana_valida = True
        except VentanaIncompatible:
            bloqueos.append(VENTANA_GLOBAL)
        except (KeyError, ValueError):
            errores.append("Ventana de envío inválida")
        advertencias.extend(advertencias_de(s))
        duracion = sum(x["esperaDiasHabiles"] for x in s["pasos"])
    if seg["estado"] != "aprobado":
        bloqueos.append(SEGMENTO_SIN_APROBAR)
    if uno(c, "SELECT archivada_en FROM secuencias WHERE id=%s", (cam["secuencia_id"],))[
        "archivada_en"
    ]:
        bloqueos.append(SECUENCIA_ARCHIVADA)
    ids = elegibles_ids(c, seg["reglas"])
    if s is not None and ids:
        usadas = {
            m
            for x in s["pasos"]
            for m in re.findall(r"\{\{(\w+)\}\}", x["cuerpo"] + (x.get("asunto") or ""))
        }
        verificados = datos_verificados(c, ids)
        for var in VARIABLES_DATOS:
            faltan = len(ids) - verificados[var]
            if var in usadas and faltan > 0:
                advertencias.append(
                    f"{{{{{var}}}}}: {faltan} de {len(ids)} contactos usarán el texto de respaldo"
                )
    gmail = uno(c, "SELECT estado FROM integraciones WHERE id='gmail'", (), False)
    gmail_ok = bool(gmail) and gmail["estado"] == "conectada"
    if not gmail_ok:
        advertencias.append("Gmail no está conectado: podrá aprobar, pero no activar la campaña")
    return {
        "puedeAprobar": not bloqueos and not errores,
        "bloqueos": bloqueos,
        "errores": errores,
        "advertencias": advertencias,
        "elegibles": len(ids),
        "segmento": {
            "id": seg["id"],
            "nombre": seg["nombre"],
            "estado": seg["estado"],
            "version": seg["version"],
        },
        "ventanaValida": ventana_valida,
        "gmailConectado": gmail_ok,
        "duracionDiasHabiles": duracion,
        "topeDiario": cam["tope_diario"],
    }


# ---------------------------------------------------------------------- versiones


def notas_editadas(anterior: Any, nueva: dict[str, Any]) -> dict[str, Any] | None:
    """Conserva notasAgente de la versión anterior y marca los correos que cambiaron."""
    if not isinstance(anterior, dict) or not isinstance(anterior.get("notasAgente"), dict):
        return None
    notas = anterior["notasAgente"]
    previa = normalizar(anterior) or {"pasos": []}
    por_orden = {
        int(n["orden"]): dict(n)
        for n in notas.get("pasos", [])
        if isinstance(n, dict) and isinstance(n.get("orden"), int)
    }
    claves = (
        "asunto",
        "cuerpo",
        "esperaDiasHabiles",
        "mismoHilo",
        "variables",
        "formato",
        "cuerpoHtml",
    )
    pasos = []
    for i, paso in enumerate(nueva["pasos"], 1):
        antes = previa["pasos"][i - 1] if i <= len(previa["pasos"]) else None
        nota = por_orden.get(i, {"orden": i})
        if antes is None or any(paso.get(k) != antes.get(k) for k in claves):
            nota = {**nota, "orden": i, "editadoPorAdmin": True}
        pasos.append(nota)
    return {**{k: v for k, v in notas.items() if k != "pasos"}, "pasos": pasos}


def contenido_editado(c: Conn, p: Row, contenido: Any) -> dict[str, Any]:
    """Valida el contenido enviado por el administrador; 400 con todos los errores."""
    if p["tipo"] != "campana":
        raise HTTPException(400, "Solo las rutas de correo admiten editar el contenido")
    s = normalizar(contenido)
    if s is None:
        raise HTTPException(400, "La ruta no tiene el formato esperado")
    marca = uno(c, "SELECT valor FROM configuracion WHERE clave='marca'")["valor"]
    errores = validar_secuencia(s, marca["prohibidas"])
    if errores:
        raise HTTPException(400, ". ".join(errores))
    anterior = uno(
        c,
        "SELECT contenido FROM propuesta_versiones WHERE propuesta_id=%s AND version=%s",
        (p["id"], p["version_actual"]),
    )["contenido"]
    notas = notas_editadas(anterior, s)
    return {**s, "notasAgente": notas} if notas else s


def previsualizar(c: Conn, p: Row, body: Any) -> dict[str, Any]:
    """Renderiza una versión con un contacto, con la misma función del motor. No envía nada."""
    from app.motor.plantillas import render

    if p["tipo"] != "campana":
        raise HTTPException(400, "Solo las rutas de correo tienen vista previa")
    if not isinstance(body, dict) or not body.get("contactoId"):
        raise HTTPException(400, "Elija un contacto")
    numero = body.get("version") or p["version_actual"]
    v = uno(
        c,
        "SELECT contenido FROM propuesta_versiones WHERE propuesta_id=%s AND version=%s",
        (p["id"], numero),
        False,
    )
    if not v:
        raise HTTPException(404, "Versión no encontrada")
    s = normalizar(v["contenido"])
    if s is None:
        raise HTTPException(400, "La versión no es una ruta de correos válida")
    ct = serializar(lecturas.contacto(c, obtener(c, "contactos", str(body["contactoId"]))))
    e = serializar(lecturas.empresa(c, obtener(c, "empresas", ct["empresaId"])))
    marca = uno(c, "SELECT valor FROM configuracion WHERE clave='marca'")["valor"]
    s["pasos"] = [{**x, "id": f"paso-{i}"} for i, x in enumerate(s["pasos"], 1)]
    resultado = render(s, ct, e, marca, "[Enlace de baja al enviar]", config().diagnostico_url)
    return {"pasos": resultado["pasos"], "respaldo": resultado["respaldo"]}


def restaurar(c: Conn, p: Row, body: Any) -> Any:
    from app.api.campanas import versionar

    numero = body.get("version") if isinstance(body, dict) else None
    if isinstance(numero, bool) or not isinstance(numero, int):
        raise HTTPException(400, "Indique la versión que desea restaurar")
    if numero == p["version_actual"]:
        raise HTTPException(400, "No se puede restaurar la versión actual")
    v = uno(
        c,
        "SELECT contenido FROM propuesta_versiones WHERE propuesta_id=%s AND version=%s",
        (p["id"], numero),
        False,
    )
    if not v:
        raise HTTPException(404, "Versión no encontrada")
    # Restaurar crea un borrador: lo aprobado sigue vigente hasta aprobar de nuevo.
    versionar(c, p, v["contenido"], autor="admin")
    return {"ok": True, "version": p["version_actual"] + 1}


def comentarios_rechazo(c: Conn, propuesta_id: str) -> list[str]:
    return [
        r["comentario"]
        for r in todos(
            c,
            "SELECT comentario FROM propuesta_versiones WHERE propuesta_id=%s "
            "AND decision='rechazada' ORDER BY version",
            (propuesta_id,),
        )
        if r["comentario"]
    ]


def jsonb(valor: Any) -> Jsonb:
    return Jsonb(serializar(valor))
