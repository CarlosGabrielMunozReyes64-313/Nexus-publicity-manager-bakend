from datetime import datetime

from app.db import Conn, encolar, todos
from app.dominio.reglas import BOGOTA


def programar(c: Conn, ahora: datetime) -> None:
    from app.agentes.descubrimiento import programar_descubrimiento, programar_meta
    from app.agentes.ejecutor import proveedor_ia_listo
    from app.tareas.bitacora import limpiar

    now = ahora.astimezone(BOGOTA)
    programar_descubrimiento(c, now)
    programar_meta(c, now)
    if now.minute == 0 and now.second < 15:
        limpiar(c)
    minute = now.strftime("%Y-%m-%dT%H:%M")
    integraciones = {
        r["id"] for r in todos(c, "SELECT id FROM integraciones WHERE estado='conectada'")
    }
    if "gmail" in integraciones:
        encolar(c, "programar_envios", {}, "programar:" + minute, 3)
        if now.minute % 2 == 0:
            encolar(c, "sincronizar_gmail", {}, "gmail:" + minute, 2)
    if now.minute % 5 == 0:
        encolar(c, "liberar_tareas_colgadas", {}, "liberar:" + minute, 1)
        if "gmail" in integraciones and todos(
            c,
            "SELECT 1 FROM mensajes WHERE estado='enviando' AND conciliacion='pendiente' LIMIT 1",
        ):
            encolar(c, "conciliar_envios", {}, "conciliar:" + minute, 2)
    from app.integraciones import bajas

    if bajas.configurada() and bajas.hay_pendientes(c):
        encolar(c, "sincronizar_bajas", {}, "bajas:" + minute, 2)
    if now.hour >= 6:
        day = now.date().isoformat()
        for tipo, integracion in (
            ("sincronizar_ga4", "ga4"),
            ("sincronizar_search_console", "search_console"),
        ):
            if integracion in integraciones and (now.hour > 6 or now.minute >= 10):
                encolar(c, tipo, {}, tipo + ":" + day, 7)
        encolar(c, "buscar_duplicados", {}, "duplicados:" + day, 6)
        # Una sola tarea diaria (clave única por día) recorre las empresas por páginas;
        # el programador ya no recorre toda la tabla en cada vuelta del trabajador.
        encolar(c, "calcular_puntajes", {"fecha": day}, "puntajes:" + day, 6)
    # El informe semanal usa el proveedor de agentes activo (Claude o Gemini).
    if now.weekday() == 0 and now.hour >= 7 and proveedor_ia_listo(integraciones):
        encolar(c, "informe_semanal", {}, "informe:" + now.date().isoformat(), 8)
