from typing import Any

from fastapi import APIRouter, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.api import datos, lecturas
from app.config import config
from app.db import actualizar, encolar, obtener, todos, transaccion, uno
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
            result = todos(c, "SELECT * FROM inscripciones WHERE campana_id=%s", (ident,))
        elif recurso == "descubrimiento" and ident == "busquedas":
            result = (
                lecturas.busqueda(c, obtener(c, "busquedas_descubrimiento", parts[2]))
                if len(parts) == 3
                else [
                    lecturas.busqueda(c, r)
                    for r in todos(
                        c, "SELECT * FROM busquedas_descubrimiento ORDER BY creada_en DESC"
                    )
                ]
            )
        elif ruta == "informes/recomendaciones":
            result = todos(c, "SELECT * FROM recomendaciones ORDER BY creada_en DESC")
        elif ruta == "embudo":
            from app.api.medicion import embudo

            result = embudo(c, dict(request.query_params))
        elif ruta == "informes/metricas":
            from app.api.medicion import metricas

            result = metricas(c, dict(request.query_params))
        elif recurso == "auditoria" and not ident:
            filtros = []
            values = []
            for k, col in {
                "entidad": "entidad",
                "entidadId": "entidad_id",
                "actor": "actor",
                "desde": "fecha",
                "hasta": "fecha",
            }.items():
                if request.query_params.get(k):
                    filtros.append(
                        col
                        + (
                            ">=%s::date"
                            if k == "desde"
                            else "<(%s::date+1)"
                            if k == "hasta"
                            else "=%s"
                        )
                    )
                    values.append(request.query_params[k])
            where = " WHERE " + " AND ".join(filtros) if filtros else ""
            result = [
                lecturas.armar(c, "auditoria", r)
                for r in todos(
                    c,
                    "SELECT * FROM registro_auditoria"
                    + where
                    + " ORDER BY fecha DESC"
                    + (" LIMIT 500" if not filtros else ""),
                    tuple(values),
                )
            ]
        elif recurso in lecturas.TABLAS and len(parts) <= 2:
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
            return serializar(mutacion(c, ruta, request.method, body, admin))
    except ValidationError as exc:
        raise HTTPException(400, "Datos no válidos; revise los campos enviados") from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, "Solicitud incompleta o con formato no válido") from exc


def mutacion(c: Any, ruta: str, method: str, body: Any, admin: str) -> Any:
    p = ruta.split("/")
    recurso = p[0]
    ident = p[1] if len(p) > 1 else None
    action = p[2] if len(p) > 2 else None
    if recurso == "empresas" and (
        (not ident and method == "POST") or (ident and method == "PATCH" and not action)
    ):
        datos.guardar_empresa(c, body, ident)
    elif ruta == "contactos/importar" and method == "POST":
        for item in body["contactos"]:
            correo = "".join(item.get("correo", "").lower().split())
            if correo and uno(c, "SELECT id FROM contactos WHERE correo=%s", (correo,), False):
                continue
            datos.guardar_contacto(c, item)
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
    elif ruta == "supresiones" and method == "POST":
        datos.suprimir(c, body)
    elif (
        recurso == "configuracion"
        and method == "PUT"
        and ident in ("marca", "limites", "cliente-ideal")
    ):
        if ident == "marca":
            body = Marca.model_validate(body).model_dump(by_alias=True)
        elif ident == "limites":
            body = Limites.model_validate(body).model_dump(by_alias=True)
            if (
                body["ventana"]["desde"] >= body["ventana"]["hasta"]
                or body["separacionSegundos"][0] > body["separacionSegundos"][1]
                or min(body["separacionSegundos"]) < 1
                or any(d not in range(7) for d in body["ventana"]["dias"])
            ):
                raise HTTPException(400, "Revise el orden de horas y separación")
        elif not isinstance(body.get("criterios"), list) or any(
            not isinstance(x, str) for x in body["criterios"]
        ):
            raise HTTPException(400, "Los criterios deben ser una lista de textos")
        c.execute(
            "UPDATE configuracion SET valor=%s,actualizado_por=%s WHERE clave=%s",
            (Jsonb(body), admin, ident.replace("-", "_")),
        )
        if ident == "marca":
            from app.api.campanas import revisar_pendientes

            revisar_pendientes(c)
        if ident == "cliente-ideal":
            for e in todos(c, "SELECT id FROM empresas WHERE fusionada_en_id IS NULL"):
                encolar(c, "calcular_puntaje", {"empresaId": str(e["id"])})
    elif (
        recurso == "integraciones"
        and ident
        and action in ("conectar", "desconectar")
        and method == "POST"
    ):
        from app.integraciones.conexiones import cambiar

        cambiar(c, ident, action)
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
