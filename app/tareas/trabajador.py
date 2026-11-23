import signal
import time
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import structlog
from fastapi import HTTPException
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from psycopg.types.json import Jsonb

from app import db
from app.agentes.ejecutor import CLAUDE_LIMITE, CLAUDE_SATURADO, CLAUDE_TRANSITORIOS
from app.agentes.errores import FalloProveedor
from app.agentes.gemini import (
    CAPACIDAD,
    CUOTA_DIARIA,
    CUOTA_DIARIA_GRATIS,
    LIMITE_MINUTO,
    SATURADO,
)
from app.api.datos import ahora
from app.api.rutas_correo import SIN_ELEGIBLES
from app.config import config
from app.db import actualizar, obtener, transaccion, uno
from app.dominio.reglas import BOGOTA
from app.integraciones.conexiones import pausar_todo
from app.tareas.bitacora import RUTINARIAS, describir, evento, latido
from app.tareas.bitacora import hora as hora_bogota
from app.tareas.programador import programar

AGENTES = {
    "enriquecer_empresa",
    "descubrir_empresas",
    "clasificar_respuesta",
    "proponer_segmentos",
    "proponer_campana",
    "revisar_campana",
    "reescribir_correo",
    "informe_semanal",
}
log = structlog.get_logger()

# Saturación o límite por minuto del proveedor de IA: no depende de la tarea y suele pasar
# sola. La cola espera más (10 intentos en unas 6 horas) en lugar de rendirse a los 36
# minutos. Los demás errores conservan sus 4 intentos.
ESPERAS_SATURACION_MIN = (1, 5, 15, 30, 60, 60, 60, 60, 60)
INTENTOS_SATURACION = len(ESPERAS_SATURACION_MIN) + 1
INTENTOS_NORMALES = 4
SATURACION = (
    {SATURADO.format(status=s) for s in (500, 502, 503, 504)}
    | {CLAUDE_SATURADO.format(status=s) for s in CLAUDE_TRANSITORIOS}
    | {LIMITE_MINUTO, CAPACIDAD, CLAUDE_LIMITE}
)
# Cuota diaria de Gemini agotada: reintentar en minutos no sirve. La tarea espera al
# reinicio de la cuota (medianoche del Pacífico, según Google) sin gastar intentos, hasta
# ESPERAS_CUOTA_MAX días seguidos.
CUOTAS_DIARIAS = {CUOTA_DIARIA, CUOTA_DIARIA_GRATIS}
ESPERAS_CUOTA_MAX = 7
PACIFICO = ZoneInfo("America/Los_Angeles")
MESES = (
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre"
).split()


def tras_reinicio_de_cuota(momento: datetime) -> datetime:
    """Cinco minutos después de la próxima medianoche del Pacífico."""
    dia = momento.astimezone(PACIFICO).date() + timedelta(days=1)
    return datetime(dia.year, dia.month, dia.day, 0, 5, tzinfo=PACIFICO)


def esperar_cuota(c: db.Conn, t: db.Row, motivo: str) -> datetime | None:
    """Reprograma la tarea al reinicio de la cuota diaria. None si ya esperó demasiado."""
    esperas = int((t["datos"] or {}).get("_esperasCuota", 0))
    if esperas >= ESPERAS_CUOTA_MAX:
        return None
    reanuda = tras_reinicio_de_cuota(ahora())
    c.execute(
        "UPDATE tareas SET estado='pendiente',ejecutar_desde=%s,tomada_por=NULL,tomada_en=NULL,"
        "intentos=greatest(0,intentos-1),ultimo_error=%s,datos=datos||%s WHERE id=%s",
        (reanuda, motivo, Jsonb({"_esperasCuota": esperas + 1}), t["id"]),
    )
    if t["tipo"] == "descubrir_empresas":
        local = reanuda.astimezone(BOGOTA)
        actualizar(
            c,
            "busquedas_descubrimiento",
            t["datos"]["busquedaId"],
            {
                "estado": "en_cola",
                "error": f"{motivo}. La búsqueda seguirá sola el {local.day} de "
                f"{MESES[local.month - 1]} a las {local:%H:%M} (hora de Colombia), cuando "
                "Google reinicie la cuota.",
            },
        )
    return reanuda


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


# Errores de agentes que no se arreglan reintentando: la tarea falla de inmediato y el
# administrador ve el motivo en lugar de esperar varios minutos de reintentos inútiles.
DEFINITIVOS = {
    "Configure los precios del proveedor para el modelo seleccionado",
    "Entrada excede el presupuesto por tarea",
    "Tope por tarea alcanzado; requiere revisión",
    "Tope mensual de agentes alcanzado",
    "La ejecución requiere revisión por costo",
    "Configure GEMINI_API_KEY en el backend y el trabajador",
    "GEMINI_MODELO no válido",
    "El segmento requiere aprobación",
    "No hay contactos elegibles con base legal aprobada",
    "El Segmentador propuso reglas no válidas",
    SIN_ELEGIBLES,
}
# Prefijos de mensajes del validador de rutas (los motivos los escribe este backend).
PREFIJOS_DEFINITIVOS = (
    "Secuencia no válida; requiere revisión",
    "La ruta propuesta no cumple:",
    "La secuencia propuesta no cumple:",
    "La sugerencia no cumple:",
)


def sin_reintento(exc: Exception) -> bool:
    if isinstance(exc, FalloProveedor):
        return not exc.reintentable
    # La ruta ya se corrigió hasta dos veces dentro de la tarea; repetir cuesta y rara vez
    # cambia el resultado. El administrador puede volver a pedirla.
    return isinstance(exc, (ValueError, HTTPException)) and (
        detalle_seguro(exc) in DEFINITIVOS or detalle_seguro(exc).startswith(PREFIJOS_DEFINITIVOS)
    )


def detalle_seguro(exc: Exception) -> str:
    import re

    if isinstance(exc, FalloProveedor):
        # Siempre se arma con textos fijos del backend (sin claves ni contenido externo).
        return str(exc)
    if isinstance(exc, HTTPException) and exc.detail in (
        "Regla de segmento no válida",
        "Las reglas deben ser una lista",
    ):
        return "El Segmentador propuso reglas no válidas"
    texto = str(exc)
    if (
        isinstance(exc, ValueError)
        and texto.startswith(PREFIJOS_DEFINITIVOS)
        and re.fullmatch(r"[\w\s{}.,:;()¿?¡!'\"-]{1,1500}", texto)
    ):
        # Los motivos los escribe el validador de secuencias de este backend.
        return texto
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
        "El segmento requiere aprobación",
        "No hay contactos elegibles con base legal aprobada",
        SIN_ELEGIBLES,
    }
    if isinstance(exc, ValueError) and (
        texto in permitidos
        or re.fullmatch(
            r"Gemini no disponible \((?:[1-5][0-9]{2}|red)\); revise clave, cuota y modelo",
            texto,
        )
    ):
        return texto
    return "Error no clasificado; requiere diagnóstico adicional"


def anotar_inicio(t: db.Row) -> None:
    if t["tipo"] in RUTINARIAS:
        return
    try:
        with transaccion("proceso:trabajador") as c:
            intento = (
                f" (intento {t['intentos']} de {t['max_intentos']})" if t["intentos"] > 1 else ""
            )
            evento(c, f"{describir(c, t)}: empieza{intento}.", "info", t["id"])
    except Exception as exc:  # pragma: no cover - la bitácora nunca detiene una tarea
        log.warning("bitacora_fallida", clase=type(exc).__name__)


def procesar(t: db.Row) -> None:
    inicio = time.monotonic()
    try:
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(600 if t["tipo"] in AGENTES else 180)
        anotar_inicio(t)
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
            if t["tipo"] not in RUTINARIAS:
                segundos = round(time.monotonic() - inicio)
                evento(c, f"{describir(c, t)}: listo en {segundos} s.", "exito", t["id"])
    except Exception as exc:
        # Do not log provider payloads, addresses, credentials or email bodies.
        log.warning(
            "tarea_fallida", tipo=t["tipo"], clase=type(exc).__name__, detalle=detalle_seguro(exc)
        )
        motivo = detalle_seguro(exc)
        with transaccion("proceso:trabajador") as c:
            descripcion = describir(c, t)
            if t["tipo"] in AGENTES and motivo in CUOTAS_DIARIAS:
                reanuda = esperar_cuota(c, t, motivo)
                if reanuda:
                    evento(
                        c,
                        f"{descripcion}: {motivo}. Seguirá sola el "
                        f"{reanuda.astimezone(BOGOTA):%d/%m} a las {hora_bogota(reanuda)}.",
                        "aviso",
                        t["id"],
                    )
                    return
            saturado = t["tipo"] in AGENTES and motivo in SATURACION
            if saturado:
                c.execute(
                    "UPDATE tareas SET max_intentos=greatest(max_intentos,%s) WHERE id=%s",
                    (INTENTOS_SATURACION, t["id"]),
                )
            elif t["tipo"] in AGENTES:
                # Si antes hubo saturación, los demás errores vuelven a su tope normal.
                c.execute(
                    "UPDATE tareas SET max_intentos=least(max_intentos,greatest(intentos,%s)) "
                    "WHERE id=%s",
                    (INTENTOS_NORMALES, t["id"]),
                )
            if t["tipo"] in AGENTES and sin_reintento(exc):
                c.execute("UPDATE tareas SET max_intentos=intentos WHERE id=%s", (t["id"],))
            state = uno(
                c,
                "SELECT terminar_tarea(%s,false,%s) AS estado",
                (
                    t["id"],
                    motivo
                    if motivo != "Error no clasificado; requiere diagnóstico adicional"
                    else "La tarea no pudo completarse; revise integración y datos",
                ),
            )["estado"]
            if saturado and state == "reprogramada":
                espera = ESPERAS_SATURACION_MIN[
                    min(int(t["intentos"]), len(ESPERAS_SATURACION_MIN)) - 1
                ]
                c.execute(
                    "UPDATE tareas SET ejecutar_desde=now()+make_interval(mins=>%s) WHERE id=%s",
                    (espera, t["id"]),
                )
            fila = uno(
                c,
                "SELECT intentos,max_intentos,ejecutar_desde FROM tareas WHERE id=%s",
                (t["id"],),
            )
            if state == "reprogramada":
                evento(
                    c,
                    f"{descripcion}: {motivo}. Intento {fila['intentos']} de "
                    f"{fila['max_intentos']}; nuevo intento a las {hora_bogota(fila['ejecutar_desde'])}.",
                    "aviso",
                    t["id"],
                )
            else:
                evento(c, f"{descripcion}: falló. {motivo}.", "error", t["id"])
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
                            "error": f"No se pudo completar la búsqueda. {motivo}.",
                        },
                    )
            elif state == "reprogramada" and t["tipo"] == "descubrir_empresas":
                # La búsqueda vuelve a la cola y el administrador ve por qué y cuándo
                # será el próximo intento (sigue actualizándose en pantalla).
                cola = uno(
                    c,
                    "SELECT intentos,max_intentos,ejecutar_desde FROM tareas WHERE id=%s",
                    (t["id"],),
                )
                hora = cola["ejecutar_desde"].astimezone(BOGOTA).strftime("%H:%M")
                actualizar(
                    c,
                    "busquedas_descubrimiento",
                    t["datos"]["busquedaId"],
                    {
                        "estado": "en_cola",
                        "error": f"Intento {cola['intentos']} de {cola['max_intentos']} sin "
                        f"éxito: {motivo}. Nuevo intento a las {hora} (hora de Colombia).",
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
                latido(c, worker)
                programar(c, ahora())
                tareas = db.todos(c, "SELECT * FROM tomar_tareas(%s,1)", (worker,))
            for t in tareas:
                procesar(t)
            time.sleep(2 if tareas else 10)
    finally:
        db.cerrar()


if __name__ == "__main__":
    main()
