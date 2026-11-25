from app.db import Conn, auditar, uno
from app.integraciones.conexiones import pausar_todo


def revisar(c: Conn) -> None:
    """Revisión periódica de quejas y rebotes del día; pausa los envíos si hace falta.

    El aviso a los administradores se crea solo cuando la revisión pausa campañas que
    estaban activas. Las revisiones siguientes del mismo día (sin campañas activas) no
    repiten el aviso, y una nueva pausa el mismo día por el mismo motivo se agrupa en él.
    """
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
    causa = ""
    if r["quejas"]:
        motivo = "Queja de spam: revisión necesaria"
        causa = "queja"
        explicacion = (
            f"Se recibió {r['quejas']} queja(s) de spam hoy. Para proteger la reputación del "
            "dominio se pausaron todas las campañas activas. Revise las campañas y los "
            "contactos que se quejaron antes de reanudar."
        )
    elif r["envios"] >= 10 and 100 * r["rebotes"] / r["envios"] > lim["umbralRebotePausa"]:
        motivo = "Umbral diario de rebotes superado"
        causa = "rebotes"
        tasa = round(100 * r["rebotes"] / r["envios"], 1)
        explicacion = (
            f"Hoy rebotaron {r['rebotes']} de {r['envios']} envíos ({tasa} %), por encima del "
            f"umbral configurado ({lim['umbralRebotePausa']} %). Se pausaron todas las campañas "
            "activas. Revise la calidad de los correos antes de reanudar."
        )
    if motivo:
        pausadas = pausar_todo(c, motivo)
        auditar(c, "alerta_correo", "integraciones", "gmail")
        if pausadas:
            from app.notificaciones import notificar_responsables

            dia = uno(c, "SELECT (now() AT TIME ZONE 'America/Bogota')::date AS d")["d"]
            notificar_responsables(
                c,
                "pausa_envios",
                f"Envíos en pausa: {motivo.split(':')[0].lower()}",
                f"{explicacion} Campañas pausadas: {pausadas}.",
                "/campanas",
                f"pausa_correo:{causa}:{dia.isoformat()}",
            )
