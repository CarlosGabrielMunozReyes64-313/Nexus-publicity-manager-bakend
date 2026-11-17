from app.api.campanas import nueva_propuesta
from app.api.lecturas import secuencia
from app.config import config
from app.db import Conn, insertar, uno
from app.esquemas.serializar import serializar


def cargar(c: Conn) -> None:
    if config().app_env != "development":
        raise RuntimeError("Datos de demostración solo en desarrollo")
    if uno(c, "SELECT id FROM empresas WHERE dominio='demo.nexus.example'", (), False):
        return
    e = insertar(
        c,
        "empresas",
        {
            "razon_social": "Empresa de demostración SAS",
            "municipio": "Palmira",
            "sector": "Servicios",
            "tamano": "11-50",
            "dominio": "demo.nexus.example",
        },
    )
    insertar(
        c,
        "contactos",
        {
            "empresa_id": e["id"],
            "nombre": "Contacto ficticio",
            "correo": "persona@demo.nexus.example",
            "origen": "manual",
        },
    )
    seg = insertar(c, "segmentos", {"nombre": "Demostración por revisar", "propuesto_por": "admin"})
    seq = insertar(
        c,
        "secuencias",
        {
            "nombre": "Diagnóstico RSE",
            "objetivo": "Invitar al diagnóstico",
            "llamada_accion": "Completar diagnóstico",
        },
    )
    insertar(
        c,
        "pasos",
        {
            "secuencia_id": seq["id"],
            "orden": 1,
            "asunto": "Diagnóstico para {{empresa}}",
            "cuerpo": "Hola {{nombre}}, le invitamos a conocer el diagnóstico RSE: {{enlace}}\n{{firma}}\n{{baja}}",
        },
    )
    cam = insertar(
        c,
        "campanas",
        {
            "nombre": "Piloto ficticio",
            "segmento_id": seg["id"],
            "secuencia_id": seq["id"],
            "estado": "en_revision",
        },
    )
    nueva_propuesta(
        c,
        "estratega_redactor",
        "campana",
        "Revisar piloto de demostración",
        serializar(secuencia(c, seq)),
        {"objetivo_campana_id": cam["id"]},
    )


def reiniciar(c: Conn) -> None:
    from fastapi import HTTPException

    from app.integraciones.conexiones import pausar_todo

    if config().app_env != "development":
        raise HTTPException(404, "Ruta no encontrada")
    c.execute("SELECT pg_advisory_xact_lock(70717)")
    if uno(c, "SELECT id FROM tareas WHERE estado='en_proceso' LIMIT 1", (), False):
        raise HTTPException(
            409, "Espere a que terminen las tareas antes de reiniciar la demostración"
        )
    pausar_todo(c, "Reinicio de demostración")
    c.execute(
        "UPDATE integraciones SET estado='desconectada',credenciales_cifradas=NULL,cursor_sincronizacion=NULL,ultima_sincronizacion=NULL"
    )
    c.execute("UPDATE campanas SET aprobacion_vigente_id=NULL,estado='en_revision'")
    # Keep append-only audit, agent execution costs and historical prompts.
    for tabla in (
        "eventos_correo",
        "enlaces_seguimiento",
        "oportunidades",
        "interacciones",
        "mensajes",
        "respuestas",
        "inscripciones",
        "aprobaciones_campana",
        "recomendaciones",
        "propuestas",
        "campanas",
        "pasos",
        "secuencias",
        "segmentos",
        "bases_legales",
        "envios_formulario",
        "personas_descubiertas",
        "evidencias",
        "resultados_descubrimiento",
        "busquedas_descubrimiento",
        "posibles_duplicados",
        "verificaciones_correo",
        "puntajes",
        "senales",
        "cambios_etapa",
        "supresiones",
        "contactos",
        "empresas",
        "tareas",
        "metricas_web_diarias",
        "consultas_busqueda_diarias",
    ):
        c.execute("DELETE FROM " + tabla)
    cargar(c)
