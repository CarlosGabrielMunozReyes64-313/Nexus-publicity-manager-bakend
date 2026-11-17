from app.db import Conn, auditar, uno
from app.integraciones.conexiones import pausar_todo


def revisar(c: Conn) -> None:
    r = uno(
        c,
        """SELECT (SELECT count(*) FROM mensajes WHERE estado='enviado' AND
        (enviado_en AT TIME ZONE 'America/Bogota')::date=(now() AT TIME ZONE 'America/Bogota')::date) AS envios,
        count(DISTINCT mensaje_id) FILTER(WHERE tipo='rebote_duro') AS rebotes,
        count(*) FILTER(WHERE tipo='queja') AS quejas FROM eventos_correo
        WHERE (ocurrido_en AT TIME ZONE 'America/Bogota')::date=(now() AT TIME ZONE 'America/Bogota')::date""",
    )
    lim = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
    motivo = None
    if r["quejas"]:
        motivo = "Queja de spam: revisión necesaria"
    elif r["envios"] >= 10 and 100 * r["rebotes"] / r["envios"] > lim["umbralRebotePausa"]:
        motivo = "Umbral diario de rebotes superado"
    if motivo:
        pausar_todo(c, motivo)
        auditar(c, "alerta_correo", "integraciones", "gmail")
