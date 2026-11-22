import signal
import time
from typing import Any
from uuid import uuid4

import structlog
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from app import db
from app.api.datos import ahora
from app.config import config
from app.db import actualizar, obtener, transaccion, uno
from app.integraciones.conexiones import pausar_todo
from app.tareas.programador import programar

AGENTES = {
    "enriquecer_empresa",
    "descubrir_empresas",
    "clasificar_respuesta",
    "proponer_segmentos",
    "proponer_campana",
    "revisar_campana",
    "informe_semanal",
}
log = structlog.get_logger()


def manejar(t: db.Row) -> None:
    tipo = t["tipo"]
    data = t["datos"]
    if data.get("_aplicada"):
        return
    if tipo in AGENTES:
        from app.agentes.tareas import correr

        correr(tipo, data, str(t["id"]))
        return
    if tipo == "programar_envios":
        from app.motor.envios import programar as programar_envios

        programar_envios()
        return
    if tipo == "enviar_mensaje":
        from app.motor.envios import enviar

        enviar(data["mensajeId"])
        return
    if tipo == "calcular_puntajes":
        from app.dominio.calidad import calcular_lote

        calcular_lote(str(t["id"]))
        return
    actor = {
        "verificar_correo": "proceso:verificador",
        "sincronizar_gmail": "proceso:rastreador_gmail",
        "sincronizar_ga4": "proceso:sincronizador_analitica",
        "sincronizar_search_console": "proceso:sincronizador_analitica",
    }.get(tipo, "proceso:calidad")
    with transaccion(actor, accion=tipo) as c:
        task = obtener(c, "tareas", str(t["id"]), True)
        if task["datos"].get("_aplicada"):
            return
        if tipo == "verificar_correo":
            from app.integraciones.hunter import verificar

            verificar(c, data["contactoId"])
        elif tipo == "buscar_duplicados":
            from app.dominio.calidad import duplicados

            duplicados(c, data.get("empresaId"))
        elif tipo == "calcular_puntaje":
            from app.dominio.calidad import calcular

            calcular(c, data["empresaId"])
        elif tipo == "sincronizar_gmail":
            from app.integraciones.conexiones import gmail
            from app.motor.rastreador import sincronizar

            sincronizar(c, gmail(c))
        elif tipo in ("sincronizar_ga4", "sincronizar_search_console"):
            from app.integraciones.analitica import sincronizar as sincronizar_analitica

            sincronizar_analitica(c, tipo)
        elif tipo == "liberar_tareas_colgadas":
            c.execute("SELECT liberar_tareas_colgadas()")
            c.execute(
                "UPDATE tareas SET estado='fallida',terminada_en=now(),ultimo_error='Trabajador interrumpido; límite de intentos' WHERE estado='en_proceso' AND tomada_en<now()-interval '15 minutes' AND intentos>=max_intentos"
            )
        else:
            raise ValueError("Tipo de tarea no registrado")
        c.execute(
            "UPDATE tareas SET datos=datos||'{\"_aplicada\":true}'::jsonb WHERE id=%s", (t["id"],)
        )


def timeout(signum: int, frame: Any) -> None:
    raise TimeoutError("La tarea excedió el tiempo permitido")


def detalle_seguro(exc: Exception) -> str:
    import re

    permitidos = {
        "Configure los precios del proveedor para el modelo seleccionado",
        "Entrada excede el presupuesto por tarea",
        "Tope por tarea alcanzado; requiere revisión",
        "Tope mensual de agentes alcanzado",
        "La ejecución requiere revisión por costo",
        "Configure GEMINI_API_KEY en el backend y el trabajador",
        "GEMINI_MODELO no válido",
        "Gemini devolvió una salida bloqueada o incompleta",
        "Gemini no aportó fuentes de búsqueda; no se guardaron empresas",
        "El modelo no devolvió una salida válida",
        "Tipo de tarea no registrado",
    }
    texto = str(exc)
    if isinstance(exc, ValueError) and (
        texto in permitidos
        or re.fullmatch(
            r"Gemini no disponible \((?:[1-5][0-9]{2}|red)\); revise clave, cuota y modelo",
            texto,
        )
    ):
        return texto
    return "Error no clasificado; requiere diagnóstico adicional"


def procesar(t: db.Row) -> None:
    try:
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(600 if t["tipo"] in AGENTES else 180)
        manejar(t)
        with transaccion("proceso:trabajador") as c:
            if t["tipo"] == "enviar_mensaje":
                m = obtener(c, "mensajes", t["datos"]["mensajeId"])
                if m["estado"] == "programado" and m["programado_para"] > ahora():
                    c.execute(
                        "UPDATE tareas SET estado='pendiente',ejecutar_desde=%s,tomada_por=NULL,tomada_en=NULL,intentos=greatest(0,intentos-1) WHERE id=%s",
                        (m["programado_para"], t["id"]),
                    )
                    return
            c.execute("SELECT terminar_tarea(%s,true)", (t["id"],))
    except Exception as exc:
        # Do not log provider payloads, addresses, credentials or email bodies.
        log.warning(
            "tarea_fallida", tipo=t["tipo"], clase=type(exc).__name__, detalle=detalle_seguro(exc)
        )
        with transaccion("proceso:trabajador") as c:
            state = uno(
                c,
                "SELECT terminar_tarea(%s,false,%s) AS estado",
                (t["id"], "La tarea no pudo completarse; revise integración y datos"),
            )["estado"]
            auth_error = (
                isinstance(exc, RefreshError)
                or isinstance(exc, HttpError)
                and exc.resp.status == 401
            )
            if auth_error and t["tipo"] in (
                "enviar_mensaje",
                "sincronizar_gmail",
                "programar_envios",
            ):
                actualizar(
                    c,
                    "integraciones",
                    "gmail",
                    {"estado": "error", "detalle": "Google requiere renovar la autorización"},
                )
                pausar_todo(c, "Gmail requiere renovar autorización")
            if state == "fallida":
                if t["tipo"] in ("enviar_mensaje", "sincronizar_gmail"):
                    pausar_todo(
                        c, "Se agotaron los intentos de una tarea de Gmail; revise auditoría"
                    )
                if t["tipo"] == "descubrir_empresas":
                    actualizar(
                        c,
                        "busquedas_descubrimiento",
                        t["datos"]["busquedaId"],
                        {
                            "estado": "error",
                            "error": "No se pudo completar la búsqueda; revise proveedor, presupuesto y fuentes",
                        },
                    )
    finally:
        signal.alarm(0)


def main() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    db.abrir(config().database_url.get_secret_value())
    worker = "trabajador-" + str(uuid4())
    try:
        while True:
            with transaccion("proceso:programador") as c:
                programar(c, ahora())
                tareas = db.todos(c, "SELECT * FROM tomar_tareas(%s,1)", (worker,))
            for t in tareas:
                procesar(t)
            time.sleep(2 if tareas else 10)
    finally:
        db.cerrar()


if __name__ == "__main__":
    main()
