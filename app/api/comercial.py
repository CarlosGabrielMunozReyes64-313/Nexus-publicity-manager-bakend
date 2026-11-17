from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from app.api import datos
from app.api.campanas import nueva_propuesta
from app.db import Conn, actualizar, encolar, insertar, obtener, todos, uno
from app.dominio.reglas import BOGOTA


def modificar(c: Conn, p: list[str], method: str, body: Any, admin: str) -> Any:
    recurso = p[0]
    ident = p[1] if len(p) > 1 else None
    action = p[2] if len(p) > 2 else None
    if recurso == "respuestas" and ident:
        r = obtener(c, "respuestas", ident, True)
        ct = obtener(c, "contactos", str(r["contacto_id"]), True)
        if action == "enviar-respuesta" and method == "POST":
            if uno(c, "SELECT contacto_bloqueado(%s) AS bloqueado", (ct["id"],))["bloqueado"]:
                raise HTTPException(400, "El contacto está suprimido")
            text = str(body.get("texto", "")).strip()
            if len(text) < 3:
                raise HTTPException(400, "El texto requiere al menos 3 caracteres")
            msg = insertar(
                c,
                "mensajes",
                {
                    "tipo": "respuesta_manual",
                    "contacto_id": ct["id"],
                    "en_respuesta_a": r["id"],
                    "clave_idempotencia": "manual:" + str(uuid4()),
                    "destinatario": ct["correo"],
                    "asunto": r["asunto"],
                    "cuerpo": text,
                    "gmail_thread_id": r["gmail_thread_id"],
                },
            )
            encolar(
                c, "enviar_mensaje", {"mensajeId": str(msg["id"])}, "enviar:" + str(msg["id"]), 2
            )
            actualizar(c, "respuestas", ident, {"estado": "atendida", "atendida_en": datos.ahora()})
        elif action == "atender" and method == "POST":
            actualizar(c, "respuestas", ident, {"estado": "atendida", "atendida_en": datos.ahora()})
        elif action == "reunion" and method == "POST":
            c.execute("SELECT detener_envios(ARRAY[%s]::uuid[],%s,true)", (ct["id"], "reunion"))
            insertar(
                c,
                "interacciones",
                {
                    "contacto_id": ct["id"],
                    "tipo": "reunion",
                    "resumen": "Reunión registrada",
                    "registrada_por": admin,
                },
            )
            datos.avanzar(c, ct["id"], "discovery", "reunion")
        elif not action and method == "PATCH":
            clas = body["clasificacion"]
            actualizar(
                c, "respuestas", ident, {"clasificacion": clas, "clasificacion_confirmada": True}
            )
            if clas == "baja":
                datos.suprimir(
                    c, {"correo": ct["correo"], "motivo": "Baja solicitada"}, "respuesta"
                )
                actualizar(
                    c, "respuestas", ident, {"estado": "atendida", "atendida_en": datos.ahora()}
                )
            elif clas == "no_interesado":
                c.execute(
                    "SELECT detener_envios(ARRAY[%s]::uuid[],%s,true)", (ct["id"], "respondio")
                )
                if not ct["excluido"]:
                    c.execute("SELECT set_config('nexus.evento','no_interesado',true)")
                    actualizar(c, "contactos", str(ct["id"]), {"etapa": "descartado"})
            elif clas in ("interesado", "pide_informacion", "quiere_reunion"):
                datos.avanzar(c, ct["id"], "interesado", "respuesta_positiva")
        else:
            raise HTTPException(404, "Ruta no encontrada")
    elif (
        recurso == "oportunidades"
        and not action
        and ((method == "POST" and not ident) or (method == "PATCH" and ident))
    ):
        mapping = {
            "empresaId": "empresa_id",
            "contactoId": "contacto_id",
            "etapa": "etapa",
            "reto": "reto",
            "valorEstimadoCOP": "valor_estimado_cop",
            "campanaOrigenId": "campana_origen_id",
            "respuestaOrigenId": "respuesta_origen_id",
        }
        values = {col: body[k] for k, col in mapping.items() if k in body}
        if values.get("respuesta_origen_id"):
            r = obtener(c, "respuestas", values["respuesta_origen_id"])
            values.update(contacto_id=r["contacto_id"], campana_origen_id=r["campana_id"])
        if values.get("etapa") in ("cliente", "perdido"):
            values["cerrada_en"] = datos.ahora()
        elif "etapa" in values:
            values["cerrada_en"] = None
        r = (
            actualizar(c, "oportunidades", ident, values)
            if ident
            else insertar(c, "oportunidades", values)
        )
        if r["etapa"] == "cliente":
            for ct in todos(
                c,
                "SELECT id FROM contactos WHERE empresa_id=%s AND NOT excluido",
                (r["empresa_id"],),
            ):
                datos.avanzar(c, ct["id"], "cliente", "oportunidad_ganada")
        elif r["etapa"] == "propuesta" and r["contacto_id"]:
            datos.avanzar(c, r["contacto_id"], "propuesta", "propuesta_enviada")
    elif recurso == "descubrimiento" and method == "POST":
        if ident == "busquedas" and not action:
            b = insertar(
                c,
                "busquedas_descubrimiento",
                {
                    "consulta": body["consulta"],
                    "municipios": body.get("municipios", []),
                    "sectores": body.get("sectores", []),
                    "tamano": body.get("tamano") or None,
                    "creada_por": admin,
                },
            )
            encolar(c, "descubrir_empresas", {"busquedaId": str(b["id"])})
        elif ident == "resultados" and action and len(p) == 4 and p[3] in ("aceptar", "descartar"):
            r = obtener(c, "resultados_descubrimiento", action, True)
            if r["estado"] != "pendiente":
                raise HTTPException(400, "Resultado no disponible")
            values = {"estado": "descartado", "decidido_por": admin, "decidido_en": datos.ahora()}
            if p[3] == "aceptar":
                if r["posible_duplicado_de"]:
                    raise HTTPException(
                        400, "Posible duplicado: revise la empresa existente antes de aceptar"
                    )
                e = datos.guardar_empresa(
                    c,
                    {
                        "razonSocial": r["razon_social"],
                        "sector": r["sector"] or "Otro",
                        "municipio": r["municipio"] or "",
                        "sitioWeb": r["sitio_web"],
                    },
                )
                values.update(estado="aceptado", empresa_creada_id=e["id"])
                for ev in todos(c, "SELECT * FROM evidencias WHERE resultado_id=%s", (r["id"],)):
                    insertar(
                        c,
                        "evidencias",
                        {
                            "empresa_id": e["id"],
                            **{
                                k: ev[k]
                                for k in (
                                    "campo",
                                    "valor",
                                    "fuente",
                                    "url",
                                    "cita",
                                    "estado",
                                    "cita_verificada_en",
                                )
                            },
                        },
                    )
                if r["correo_corporativo"]:
                    datos.guardar_contacto(
                        c,
                        {
                            "empresaId": str(e["id"]),
                            "nombre": "Contacto corporativo",
                            "correo": r["correo_corporativo"],
                            "telefono": r["telefono"],
                            "origen": "descubrimiento",
                            "baseLegal": {"tipo": "corporativo_generico"},
                        },
                    )
            actualizar(c, "resultados_descubrimiento", action, values)
        else:
            raise HTTPException(404, "Ruta no encontrada")
    elif recurso == "informes" and ident == "costos" and method == "POST":
        costo = body.get("costoUSD")
        if isinstance(costo, bool) or not isinstance(costo, (int, float)) or costo < 0:
            raise HTTPException(400, "El costo debe ser mayor o igual a cero")
        insertar(
            c,
            "costos",
            {
                "fecha": datos.ahora().astimezone(BOGOTA).date(),
                "categoria": "otro",
                "concepto": "Registro manual",
                "monto": costo,
                "moneda": "USD",
                "registrado_por": admin,
            },
        )
    elif (
        recurso == "informes"
        and ident == "recomendaciones"
        and action
        and len(p) == 4
        and p[3] in ("aprobar", "descartar")
        and method == "POST"
    ):
        r = obtener(c, "recomendaciones", action, True)
        if r["estado"] != "pendiente":
            raise HTTPException(400, "La recomendación ya fue decidida")
        values = {"estado": "descartada", "decidida_en": datos.ahora()}
        if p[3] == "aprobar":
            prop = nueva_propuesta(
                c,
                "analista",
                "mejora",
                r["texto"],
                {"texto": r["texto"], "sustento": r["sustento"]},
            )
            values.update(estado="aprobada", propuesta_id=prop["id"])
        actualizar(c, "recomendaciones", action, values)
    else:
        raise HTTPException(404, "Ruta no encontrada")
    return {"ok": True}
