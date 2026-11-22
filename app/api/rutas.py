from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.api import auditoria, datos, lecturas
from app.config import config
from app.db import actualizar, encolar, obtener, todos, transaccion, uno
from app.dominio.reglas import ventanas_compatibles
from app.esquemas.entradas import Limites, Marca
from app.esquemas.serializar import serializar
from app.seguridad.auth import administrador

router = APIRouter(prefix="/api")


@router.get("/{ruta:path}")
def leer(ruta: str, request: Request) -> Any:
    admin = administrador(request)
    result: Any
    parts = ruta.split("/")
    recurso = parts[0]
    ident = parts[1] if len(parts) > 1 else None
    params = request.query_params
    with transaccion("admin", admin) as c:
        if ruta == "panel/resumen":
            result = uno(c, "SELECT * FROM v_resumen_panel")
        elif recurso == "configuracion" and ident in ("marca", "limites", "cliente-ideal"):
            result = uno(
                c, "SELECT valor FROM configuracion WHERE clave=%s", (ident.replace("-", "_"),)
            )["valor"]
        elif recurso == "contactos" and len(parts) == 3 and parts[2] == "interacciones":
            obtener(c, "contactos", str(ident))
            result = todos(
                c, "SELECT * FROM v_linea_tiempo WHERE contacto_id=%s ORDER BY fecha DESC", (ident,)
            )
        elif recurso == "campanas" and len(parts) == 3 and parts[2] == "inscripciones":
            obtener(c, "campanas", str(ident))
            result = todos(
                c,
                "SELECT * FROM inscripciones WHERE campana_id=%s ORDER BY inscrita_en",
                (ident,),
            )
        elif recurso == "descubrimiento" and ident == "busquedas":
            result = (
                lecturas.busqueda(c, obtener(c, "busquedas_descubrimiento", parts[2]))
                if len(parts) == 3
                else lecturas.busquedas(
                    c, todos(c, "SELECT * FROM busquedas_descubrimiento ORDER BY creada_en DESC")
                )
            )
        elif ruta == "informes/recomendaciones":
            result = todos(c, "SELECT * FROM recomendaciones ORDER BY creada_en DESC")
        elif ruta == "embudo":
            from app.api.medicion import embudo

            result = embudo(c, dict(params))
        elif ruta == "informes/metricas":
            from app.api.medicion import metricas

            result = metricas(c, dict(params))
        elif ruta == "propuestas/solicitudes":
            from app.api.rutas_correo import solicitudes as solicitudes_rutas

            result = solicitudes_rutas(c)
        elif ruta == "propuestas/rutas-correo/segmentos":
            from app.api.rutas_correo import segmentos_para_ruta

            result = segmentos_para_ruta(c)
        elif recurso == "propuestas" and len(parts) == 3 and parts[2] in ("revision", "elegibles"):
            from app.api import rutas_correo

            prop = obtener(c, "propuestas", parts[1])
            result = (
                rutas_correo.revision(c, prop)
                if parts[2] == "revision"
                else rutas_correo.elegibles_de(c, prop)
            )
        elif ruta == "agentes/solicitudes":
            from app.agentes.solicitudes import lista as solicitudes

            result = solicitudes(c)
        elif ruta == "auditoria":
            result = auditoria.lista(c, params)
        elif ruta == "auditoria/pagina":
            result = auditoria.pagina(c, params)
        elif ruta == "auditoria/exportar":
            # Se arma dentro de la transacción: todas las coincidencias, no solo 500.
            contenido = "".join(auditoria.exportar(c, params))
            return Response(
                contenido,
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": 'attachment; filename="nexus-auditoria.csv"'},
            )
        elif recurso in lecturas.TABLAS and len(parts) <= 2 and recurso != "auditoria":
            if not ident and params.get("limite"):
                # Paginación opcional del servidor; sin parámetros se conserva el
                # contrato original (arreglo completo).
                try:
                    limite = max(1, min(500, int(params["limite"])))
                    desplazamiento = max(0, int(params.get("desplazamiento") or 0))
                except ValueError as exc:
                    raise HTTPException(400, "limite y desplazamiento deben ser números") from exc
                filas = lecturas.lista(c, recurso, None, (limite, desplazamiento))
                return JSONResponse(
                    serializar(filas),
                    headers={"X-Total-Count": str(lecturas.total(c, recurso))},
                )
            result = lecturas.lista(c, recurso, ident)
        else:
            raise HTTPException(404, "Ruta no encontrada")
        return serializar(result)


@router.api_route("/{ruta:path}", methods=["POST", "PATCH", "PUT"])
async def escribir(ruta: str, request: Request) -> Any:
    admin = administrador(request)
    try:
        raw = await request.body()
        body = await request.json() if raw else {}
        with transaccion("admin", admin, request.method.lower() + ":" + ruta.split("/")[0]) as c:
            return serializar(mutacion(c, ruta, request.method, body, admin, request.query_params))
    except ValidationError as exc:
        raise HTTPException(400, "Datos no válidos; revise los campos enviados") from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, "Solicitud incompleta o con formato no válido") from exc


def _verdadero(valor: Any) -> bool:
    return str(valor).strip().lower() in ("1", "true", "si", "sí")


def guardar_limites(c: Any, body: Any, params: Mapping[str, str]) -> dict[str, Any]:
    """Valida el horario global contra las campañas aprobadas antes de guardarlo."""
    pausar = _verdadero(params.get("pausarIncompatibles", "")) or (
        isinstance(body, dict) and _verdadero(body.get("pausarIncompatibles", ""))
    )
    valor = Limites.model_validate(body).model_dump(by_alias=True)
    if (
        valor["ventana"]["desde"] >= valor["ventana"]["hasta"]
        or valor["separacionSegundos"][0] > valor["separacionSegundos"][1]
        or min(valor["separacionSegundos"]) < 1
        or any(d not in range(7) for d in valor["ventana"]["dias"])
    ):
        raise HTTPException(400, "Revise el orden de horas y separación")
    valor["ventana"]["dias"] = sorted(set(valor["ventana"]["dias"]))
    conflictos = [
        cam
        for cam in todos(
            c,
            """SELECT ca.id,ca.nombre,ca.estado,a.contenido_congelado FROM campanas ca
            JOIN aprobaciones_campana a ON a.id=ca.aprobacion_vigente_id
            WHERE ca.estado IN ('aprobada','activa','pausada') ORDER BY ca.nombre""",
        )
        if not ventanas_compatibles(
            cam["contenido_congelado"]["secuencia"]["ventanaEnvio"], valor["ventana"]
        )
    ]
    if conflictos and not pausar:
        nombres = ", ".join(str(x["nombre"]) for x in conflictos)
        raise HTTPException(
            409,
            "El nuevo horario no tiene días u horas en común con la ventana aprobada de: "
            + nombres
            + ". Ajuste esas secuencias o confirme que desea pausar las campañas afectadas.",
        )
    return {"valor": valor, "conflictos": conflictos}


def mutacion(
    c: Any,
    ruta: str,
    method: str,
    body: Any,
    admin: str,
    params: Mapping[str, str] | None = None,
) -> Any:
    params = params or {}
    p = ruta.split("/")
    recurso = p[0]
    ident = p[1] if len(p) > 1 else None
    action = p[2] if len(p) > 2 else None
    if recurso == "empresas" and (
        (not ident and method == "POST") or (ident and method == "PATCH" and not action)
    ):
        datos.guardar_empresa(c, body, ident)
    elif ruta == "contactos/importar" and method == "POST":
        return {"ok": True, **datos.importar_contactos(c, body)}
    elif (
        recurso == "contactos"
        and not action
        and ((not ident and method == "POST") or (ident and method == "PATCH"))
    ):
        datos.guardar_contacto(c, body, ident)
    elif recurso == "contactos" and ident and method == "POST" and action == "excluir":
        r = obtener(c, "contactos", ident, True)
        motivo = str(body.get("motivo", "")).strip()
        if len(motivo) < 3:
            raise HTTPException(400, "El motivo requiere al menos 3 caracteres")
        if r["correo"]:
            datos.suprimir(c, {"correo": r["correo"], "motivo": motivo})
        else:
            actualizar(
                c,
                "contactos",
                ident,
                {
                    "excluido": True,
                    "motivo_exclusion": motivo,
                    "excluido_en": datos.ahora(),
                    "etapa": "excluido",
                },
            )
            c.execute("SELECT detener_envios(ARRAY[%s]::uuid[],%s,true)", (ident, "manual"))
    elif (
        len(p) == 4
        and recurso == "contactos"
        and p[2:] == ["base-legal", "aprobar"]
        and method == "POST"
    ):
        datos.aprobar_legal(c, str(ident), admin)
    elif recurso == "duplicados" and ident and action == "resolver" and method == "POST":
        datos.fusionar(c, ident, body, admin)
    elif recurso == "duplicados" and ident and action == "descartar" and method == "POST":
        datos.descartar_duplicado(c, ident, admin)
    elif ruta == "supresiones" and method == "POST":
        datos.suprimir(c, body)
    elif (
        recurso == "configuracion"
        and method == "PUT"
        and ident in ("marca", "limites", "cliente-ideal")
    ):
        pausadas: list[str] = []
        revision: dict[str, Any] = {"conflictos": []}
        if ident == "marca":
            body = Marca.model_validate(body).model_dump(by_alias=True)
        elif ident == "limites":
            revision = guardar_limites(c, body, params)
            body = revision["valor"]
        elif not isinstance(body.get("criterios"), list) or any(
            not isinstance(x, str) for x in body["criterios"]
        ):
            raise HTTPException(400, "Los criterios deben ser una lista de textos")
        else:
            body = {"criterios": [x.strip() for x in body["criterios"] if x.strip()]}
        c.execute(
            "UPDATE configuracion SET valor=%s,actualizado_por=%s WHERE clave=%s",
            (Jsonb(body), admin, ident.replace("-", "_")),
        )
        if ident == "limites":
            from app.motor.pausas import pausar_campana

            for cam in revision["conflictos"]:
                pausar_campana(c, str(cam["id"]), "pausa_por_cambio_de_horario")
                pausadas.append(str(cam["nombre"]))
            return {"ok": True, "pausadas": pausadas}
        if ident == "marca":
            from app.api.campanas import revisar_pendientes

            revisar_pendientes(c)
        if ident == "cliente-ideal":
            encolar(c, "calcular_puntajes", {"motivo": "cliente_ideal"})
    elif (
        recurso == "integraciones"
        and ident
        and action in ("conectar", "desconectar")
        and method == "POST"
    ):
        from app.integraciones.conexiones import cambiar

        return {"ok": True, **cambiar(c, ident, action)}
    elif ruta == "propuestas/rutas-correo" and method == "POST":
        from app.api.rutas_correo import pedir

        return pedir(c, body)
    elif (
        recurso == "propuestas"
        and ident == "solicitudes"
        and len(p) == 4
        and p[3] == "reintentar"
        and method == "POST"
    ):
        from app.api.rutas_correo import reintentar

        return reintentar(c, p[2])
    elif ruta == "segmentos/proponer" and method == "POST":
        from app.agentes.solicitudes import pedir_segmentos

        return {"ok": True, **pedir_segmentos(c)}
    elif recurso == "segmentos" and ident and action == "proponer-campana" and method == "POST":
        from app.agentes.solicitudes import pedir_campana

        return {"ok": True, **pedir_campana(c, ident)}
    elif recurso in ("segmentos", "secuencias", "propuestas", "campanas"):
        from app.api.campanas import modificar

        return modificar(c, p, method, body, admin)
    elif recurso in ("respuestas", "oportunidades", "descubrimiento", "informes"):
        from app.api.comercial import modificar as modificar_comercial

        return modificar_comercial(c, p, method, body, admin)
    elif ruta == "dev/reset" and method == "POST" and config().app_env == "development":
        from app.demo import reiniciar

        reiniciar(c)
    else:
        raise HTTPException(404, "Ruta no encontrada")
    return {"ok": True}
