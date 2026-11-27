"""Plantillas de correo reutilizables.

Una plantilla tiene nombre, asunto, cuerpo en texto simple y, opcionalmente, una versión con
formato (HTML limpio con nh3, igual que los pasos). Al usarla en un paso se COPIA su
contenido al borrador de la secuencia: lo aprobado queda congelado en la aprobación, así que
editar o archivar la plantilla después nunca cambia un correo aprobado ni uno programado.

La vista previa usa el mismo render que el motor (app/motor/plantillas.py) con datos de
ejemplo o con un contacto real elegido, y devuelve las dos versiones (HTML y texto).
"""

from typing import Any

from fastapi import HTTPException

from app.api import datos, lecturas
from app.config import config
from app.db import Conn, Row, actualizar, auditar, insertar, obtener, todos, uno
from app.esquemas.serializar import serializar
from app.motor.html import con_plantilla, preparar_pasos
from app.motor.plantillas import (
    VARIABLES_ADMITIDAS,
    con_pie_de_baja,
    render,
    variables_de,
    variables_no_admitidas,
)

EJEMPLO_CONTACTO = {
    "id": "ejemplo",
    "nombre": "Laura Gómez",
    "cargo": "Gerente de compras",
    "evidencias": [
        {"campo": "nombre", "valor": "Laura Gómez", "estado": "verificado"},
        {"campo": "cargo", "valor": "Gerente de compras", "estado": "verificado"},
    ],
}
EJEMPLO_EMPRESA = {
    "razonSocial": "Agroindustrias del Valle S.A.S.",
    "municipio": "Palmira",
    "senales": [{"descripcion": "provee a ingenios azucareros de la región"}],
    "evidencias": [
        {
            "campo": "razonSocial",
            "valor": "Agroindustrias del Valle S.A.S.",
            "estado": "verificado",
        },
        {"campo": "municipio", "valor": "Palmira", "estado": "verificado"},
    ],
}


def _validar(body: Any) -> Row:
    if not isinstance(body, dict):
        raise HTTPException(400, "Datos de plantilla no válidos")
    nombre = " ".join(str(body.get("nombre") or "").split())
    asunto = str(body.get("asunto") or "").strip()
    cuerpo = str(body.get("cuerpo") or "")
    formato = body.get("formato") or "texto"
    html = body.get("cuerpoHtml") if formato == "html" else None
    if not 1 <= len(nombre) <= 120:
        raise HTTPException(400, "El nombre requiere entre 1 y 120 caracteres")
    if len(asunto) > 200:
        raise HTTPException(400, "El asunto admite hasta 200 caracteres")
    if formato not in ("texto", "html"):
        raise HTTPException(400, "Formato no válido")
    if not 1 <= len(cuerpo.strip()) or len(cuerpo) > 3000:
        raise HTTPException(400, "El cuerpo requiere entre 1 y 3000 caracteres")
    if formato == "html":
        if not str(html or "").strip():
            raise HTTPException(400, "Falta el texto con formato")
        # Misma limpieza de HTML que los pasos de una secuencia.
        paso = preparar_pasos(
            {"pasos": [{"orden": 1, "cuerpo": cuerpo, "formato": "html", "cuerpoHtml": html}]}
        )["pasos"][0]
        html = paso.get("cuerpoHtml")
        cuerpo = paso.get("cuerpo") or cuerpo
    errores = variables_no_admitidas(
        {"pasos": [{"asunto": asunto, "cuerpo": cuerpo, "cuerpoHtml": html, "variables": []}]}
    )
    if errores:
        raise HTTPException(400, errores[0].replace("Correo 1: ", ""))
    return {
        "nombre": nombre,
        "asunto": asunto,
        "cuerpo": cuerpo,
        "formato": formato,
        "cuerpo_html": html,
    }


def armar(filas: list[Row]) -> list[Row]:
    return [
        {
            **f,
            "archivada": f["archivada_en"] is not None,
            "variables": variables_de(f["asunto"], f["cuerpo"], f["cuerpo_html"]),
            "sinBaja": "{{baja}}" not in f["cuerpo"],
        }
        for f in filas
    ]


def lista(c: Conn, params: Any) -> list[Row]:
    archivadas = str(params.get("archivadas", "")).lower() == "true"
    return armar(
        todos(
            c,
            "SELECT * FROM plantillas"
            + ("" if archivadas else " WHERE archivada_en IS NULL")
            + " ORDER BY lower(nombre), id",
        )
    )


def detalle(c: Conn, ident: str) -> Row:
    return armar([obtener(c, "plantillas", ident)])[0]


def previsualizar(c: Conn, body: Any, ident: str | None = None) -> dict[str, Any]:
    """Render con datos de ejemplo (o un contacto elegido). No guarda nada."""
    body = body if isinstance(body, dict) else {}
    if ident and not body.get("cuerpo"):
        t = obtener(c, "plantillas", ident)
        v = {k: t[k] for k in ("nombre", "asunto", "cuerpo", "formato", "cuerpo_html")}
    else:
        v = _validar({"nombre": body.get("nombre") or "Vista previa", **body})
    marca = uno(c, "SELECT valor FROM configuracion WHERE clave='marca'")["valor"]
    if body.get("contactoId"):
        ct = serializar(lecturas.contacto(c, obtener(c, "contactos", str(body["contactoId"]))))
        e = serializar(lecturas.empresa(c, obtener(c, "empresas", ct["empresaId"])))
    else:
        ct, e = EJEMPLO_CONTACTO, EJEMPLO_EMPRESA
    paso = {
        "id": "vista",
        "orden": 1,
        "asunto": v["asunto"],
        "cuerpo": v["cuerpo"],
        "formato": v["formato"],
        "cuerpoHtml": v["cuerpo_html"],
        "variables": [],
        "esperaDiasHabiles": 0,
        "mismoHilo": False,
    }
    r = render(
        {"pasos": [paso]}, ct, e, marca, "https://ejemplo.invalid/baja/…", config().diagnostico_url
    )
    p = r["pasos"][0]
    html = p.get("cuerpoHtml")
    return {
        "asunto": p.get("asunto") or "",
        "texto": p["cuerpo"],
        "html": con_plantilla(html) if html else None,
        "respaldo": r["respaldo"],
        "pieAgregado": "{{baja}}" not in v["cuerpo"],
        "variables": variables_de(v["asunto"], v["cuerpo"], v["cuerpo_html"]),
        "admitidas": list(VARIABLES_ADMITIDAS),
    }


def para_paso(c: Conn, ident: str) -> Row:
    """Contenido listo para copiar en un paso: garantiza {{firma}} y {{baja}}."""
    t = obtener(c, "plantillas", ident)
    if t["archivada_en"]:
        raise HTTPException(409, "La plantilla está archivada")
    cuerpo = t["cuerpo"]
    if "{{firma}}" not in cuerpo:
        cuerpo = cuerpo.rstrip() + "\n\n{{firma}}"
    cuerpo = con_pie_de_baja(cuerpo)
    html = t["cuerpo_html"]
    if html:
        if "{{firma}}" not in html:
            html += "<p>{{firma}}</p>"
        html = con_pie_de_baja(html, html=True)
    return {
        "plantillaId": str(t["id"]),
        "plantillaVersion": t["version"],
        "asunto": t["asunto"],
        "cuerpo": cuerpo,
        "formato": t["formato"],
        "cuerpoHtml": html,
    }


def modificar(c: Conn, p: list[str], method: str, body: Any) -> Any:
    if len(p) == 1 and method == "POST":
        t = insertar(c, "plantillas", _validar(body))
        auditar(c, "crear_plantilla", "plantillas", str(t["id"]))
        return {"ok": True, "id": str(t["id"])}
    if len(p) == 2 and p[1] == "previsualizar" and method == "POST":
        return previsualizar(c, body)
    ident = p[1]
    accion = p[2] if len(p) > 2 else None
    t = obtener(c, "plantillas", ident, True)
    if accion is None and method == "PUT":
        if t["archivada_en"]:
            raise HTTPException(409, "Restaure la plantilla antes de editarla")
        actualizar(c, "plantillas", ident, {**_validar(body), "version": t["version"] + 1})
        return {"ok": True, "version": t["version"] + 1}
    if method != "POST":
        raise HTTPException(404, "Ruta no encontrada")
    if accion == "previsualizar":
        return previsualizar(c, body, ident)
    if accion == "duplicar":
        n = insertar(
            c,
            "plantillas",
            {
                **{k: t[k] for k in ("asunto", "cuerpo", "formato", "cuerpo_html")},
                "nombre": ("Copia de " + t["nombre"])[:120],
            },
        )
        auditar(c, "duplicar_plantilla", "plantillas", str(n["id"]))
        return {"ok": True, "id": str(n["id"])}
    if accion == "archivar":
        actualizar(c, "plantillas", ident, {"archivada_en": datos.ahora()})
        auditar(c, "archivar_plantilla", "plantillas", ident)
        return {"ok": True}
    if accion == "restaurar":
        actualizar(c, "plantillas", ident, {"archivada_en": None})
        return {"ok": True}
    raise HTTPException(404, "Ruta no encontrada")
