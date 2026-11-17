from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.api.lecturas import contacto, empresa
from app.db import Conn, Row, actualizar, encolar, insertar, obtener, uno
from app.dominio.reglas import ETAPAS, dominio, telefono
from app.esquemas.entradas import Contacto, Empresa
from app.esquemas.serializar import serializar


def ahora() -> datetime:
    return datetime.now(timezone.utc)


def avanzar(c: Conn, ident: Any, etapa: str, evento: str) -> None:
    r = obtener(c, "contactos", str(ident), True)
    if r["excluido"] or r["etapa"] not in ETAPAS:
        return
    if ETAPAS.index(etapa) > ETAPAS.index(r["etapa"]):
        c.execute("SELECT set_config('nexus.evento',%s,true)", (evento,))
        actualizar(c, "contactos", str(ident), {"etapa": etapa})


def evidencia_manual(c: Conn, tipo: str, ident: Any, valores: Row) -> None:
    for k, v in valores.items():
        if k in (
            "id",
            "empresaId",
            "estadoCuenta",
            "origen",
            "etapa",
            "baseLegal",
            "evidencias",
        ) or isinstance(v, (dict, list)):
            continue
        insertar(
            c,
            "evidencias",
            {
                tipo + "_id": ident,
                "campo": k,
                "valor": None if v is None else str(v),
                "fuente": "manual",
                "estado": "pendiente",
            },
        )


def guardar_empresa(c: Conn, body: Row, ident: str | None = None) -> Row:
    anterior = obtener(c, "empresas", ident, True) if ident else {}
    base = serializar(empresa(c, anterior)) if ident else {}
    parsed = Empresa.model_validate({**base, **body})
    values = parsed.model_dump()
    if values.get("sitio_web"):
        values["dominio"] = dominio(values["sitio_web"])
    elif values.get("dominio"):
        values["dominio"] = dominio(values["dominio"])
    for k in ("nit", "sitio_web", "dominio"):
        values[k] = values.get(k) or None
    r = actualizar(c, "empresas", ident, values) if ident else insertar(c, "empresas", values)
    changes = {k: v for k, v in serializar(values).items() if v != base.get(k)}
    evidencia_manual(c, "empresa", r["id"], changes)
    if r["sitio_web"]:
        encolar(c, "enriquecer_empresa", {"empresaId": str(r["id"])})
    encolar(c, "buscar_duplicados", {"empresaId": str(r["id"])})
    encolar(c, "calcular_puntaje", {"empresaId": str(r["id"])})
    return r


def legal(c: Conn, cid: Any, body: Row) -> None:
    tipo = body.get("tipo", "pendiente")
    if tipo == "autorizacion_formulario":
        raise HTTPException(400, "La autorización de formulario debe proceder del webhook firmado")
    c.execute(
        "UPDATE bases_legales SET vigente=false,revocada_en=now(),motivo_revocacion='Reemplazada' WHERE contacto_id=%s AND vigente",
        (cid,),
    )
    insertar(
        c,
        "bases_legales",
        {
            "contacto_id": cid,
            "tipo": tipo,
            "texto": body.get("texto"),
            "prueba": body.get("prueba"),
            "obtenida_en": body.get("fecha"),
        },
    )


def guardar_contacto(c: Conn, body: Row, ident: str | None = None) -> Row:
    old = obtener(c, "contactos", ident, True) if ident else {}
    base = serializar(contacto(c, old)) if ident else {}
    parsed = Contacto.model_validate({**base, **body})
    values = parsed.model_dump(exclude={"base_legal"})
    if values["correo"]:
        values["correo"] = "".join(values["correo"].lower().split())
    else:
        values["correo"] = None
    if values["telefono"]:
        values["telefono"] = telefono(values["telefono"])
    obtener(c, "empresas", values["empresa_id"])
    changed = not ident or values["correo"] != old["correo"]
    if changed:
        values["estado_correo"] = "desconocido"
    if ident and "etapa" in body:
        values["etapa"] = body["etapa"]
        c.execute("SELECT set_config('nexus.evento','cambio_manual',true)")
    r = actualizar(c, "contactos", ident, values) if ident else insertar(c, "contactos", values)
    if not ident or "baseLegal" in body:
        legal(c, r["id"], parsed.base_legal.model_dump(by_alias=True))
    evidencia_manual(
        c, "contacto", r["id"], {k: v for k, v in serializar(values).items() if v != base.get(k)}
    )
    if changed and r["correo"]:
        encolar(c, "verificar_correo", {"contactoId": str(r["id"])})
    return r


def aprobar_legal(c: Conn, ident: str, admin: str) -> None:
    obtener(c, "contactos", ident, True)
    b = uno(
        c,
        "SELECT * FROM bases_legales WHERE contacto_id=%s AND vigente FOR UPDATE",
        (ident,),
        False,
    )
    if (
        not b
        or b["tipo"] == "pendiente"
        or not (b["texto"] or "").strip()
        or not (b["prueba"] or "").strip()
    ):
        raise HTTPException(400, "Registre tipo, texto y prueba de la base legal antes de aprobar")
    actualizar(c, "bases_legales", str(b["id"]), {"aprobada_por": admin, "aprobada_en": ahora()})
    if uno(c, "SELECT motivo_no_elegible(%s) AS motivo", (ident,))["motivo"] is None:
        avanzar(c, ident, "contactable", "contactable")


def suprimir(c: Conn, body: Row, origen: str = "manual") -> None:
    correo, dom = body.get("correo"), body.get("dominio")
    if bool(correo) == bool(dom):
        raise HTTPException(400, "Indique un correo o dominio")
    motivo = str(body.get("motivo", "")).strip()
    if len(motivo) < 3:
        raise HTTPException(400, "El motivo requiere al menos 3 caracteres")
    c.execute(
        "INSERT INTO supresiones(correo,dominio,motivo,origen) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (correo.strip().lower() if correo else None, dominio(dom) if dom else None, motivo, origen),
    )


def fusionar(c: Conn, ident: str, body: Row, admin: str) -> None:
    d = obtener(c, "posibles_duplicados", ident, True)
    if d["estado"] != "pendiente":
        raise HTTPException(400, "Duplicado ya resuelto")
    a = obtener(c, "empresas", str(d["principal_id"]), True)
    b = obtener(c, "empresas", str(d["duplicado_id"]), True)
    campos = {
        "razonSocial": "razon_social",
        "sector": "sector",
        "municipio": "municipio",
        "tamano": "tamano",
        "sitioWeb": "sitio_web",
        "dominio": "dominio",
    }
    if any(k not in campos or v not in ("principal", "duplicado") for k, v in body.items()):
        raise HTTPException(400, "Selección de campos no válida")
    actualizar(c, "empresas", str(b["id"]), {"fusionada_en_id": a["id"]})
    actualizar(
        c,
        "empresas",
        str(a["id"]),
        {campos[k]: b[campos[k]] for k, v in body.items() if v == "duplicado"},
    )
    for tabla in ("contactos", "oportunidades", "senales", "evidencias"):
        c.execute(f"UPDATE {tabla} SET empresa_id=%s WHERE empresa_id=%s", (a["id"], b["id"]))
    actualizar(
        c,
        "posibles_duplicados",
        ident,
        {
            "estado": "fusionado",
            "campos_elegidos": Jsonb(body),
            "resuelto_por": admin,
            "resuelto_en": ahora(),
        },
    )
