from datetime import datetime

from app.db import Conn, encolar, todos
from app.dominio.reglas import BOGOTA


def programar(c: Conn, ahora: datetime) -> None:
    now = ahora.astimezone(BOGOTA)
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
    if now.hour >= 6:
        day = now.date().isoformat()
        for tipo, integracion in (
            ("sincronizar_ga4", "ga4"),
            ("sincronizar_search_console", "search_console"),
        ):
            if integracion in integraciones and (now.hour > 6 or now.minute >= 10):
                encolar(c, tipo, {}, tipo + ":" + day, 7)
        encolar(c, "buscar_duplicados", {}, "duplicados:" + day, 6)
        for e in todos(c, "SELECT id FROM empresas WHERE fusionada_en_id IS NULL"):
            encolar(
                c, "calcular_puntaje", {"empresaId": str(e["id"])}, f"puntaje:{day}:{e['id']}", 6
            )
    if now.weekday() == 0 and now.hour >= 7 and "claude" in integraciones:
        encolar(c, "informe_semanal", {}, "informe:" + now.date().isoformat(), 8)
