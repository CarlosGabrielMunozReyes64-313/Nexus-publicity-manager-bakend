import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.api.lecturas import contacto, empresa
from app.db import Conn, Row, actualizar, auditar, encolar, insertar, obtener, todos, uno
from app.dominio.reglas import BOGOTA, ETAPAS, dominio, telefono
from app.esquemas.entradas import Contacto, Empresa
from app.esquemas.serializar import serializar

CORREO = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
MAX_IMPORTACION = 5000


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


def verificar_correo(c: Conn, ident: str) -> dict[str, Any]:
    """POST /contactos/:id/verificar-correo: vuelve a verificar el correo (DNS + Hunter).

    El estado solo lo cambia el verificador; aquí no se marca nada a mano.
    """
    ct = obtener(c, "contactos", ident)
    if not ct["correo"]:
        raise HTTPException(400, "El contacto no tiene correo")
    if ct["anonimizado_en"] or ct["excluido"]:
        raise HTTPException(409, "El contacto está excluido o anonimizado")
    encolar(
        c,
        "verificar_correo",
        {"contactoId": str(ct["id"])},
        f"verificar_correo:{ct['id']}:{uuid4()}",
        3,
    )
    auditar(c, "verificar_correo", "contactos", str(ct["id"]))
    return {"ok": True}


def verificar_pendientes(c: Conn) -> int:
    """Encola la verificación de los correos que nunca se pudieron verificar."""
    filas = todos(
        c,
        "SELECT id FROM contactos WHERE correo IS NOT NULL AND estado_correo='desconocido' "
        "AND anonimizado_en IS NULL AND NOT excluido",
    )
    for f in filas:
        encolar(
            c,
            "verificar_correo",
            {"contactoId": str(f["id"])},
            f"verificar_correo:{f['id']}:{uuid4()}",
            6,
        )
    return len(filas)


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


# ---------------------------------------------------------------- aprobación automática

TEXTO_AUTOMATICO = (
    "Correo de contacto publicado por {empresa} en su sitio web ({url}). Base aprobada "
    "automáticamente por la política de cumplimiento, sin revisión manual."
)


def politica_automatica(c: Conn) -> str | None:
    """Administrador que encendió la aprobación automática, o None si está apagada."""
    r = uno(
        c, "SELECT valor, actualizado_por FROM configuracion WHERE clave='cumplimiento'", (), False
    )
    if not r or not r["valor"].get("aprobacionLegalAutomatica") or not r["actualizado_por"]:
        return None
    return str(r["actualizado_por"])


def fuente_publicada(c: Conn, ct: Row) -> Row | None:
    """La evidencia verificada (con página y cita comprobada) del correo del contacto."""
    return uno(
        c,
        """SELECT url, cita, obtenido_en FROM evidencias
           WHERE url IS NOT NULL AND estado='verificado' AND lower(valor)=lower(%s)
             AND (contacto_id=%s OR (empresa_id=%s AND campo IN ('correoCorporativo','correo')))
           ORDER BY obtenido_en DESC LIMIT 1""",
        (ct["correo"], ct["id"], ct["empresa_id"]),
        False,
    )


def aprobar_automatica(c: Conn, ident: str) -> bool:
    """Si la política está encendida, llena la evidencia y aprueba la base legal del contacto.

    Solo con una prueba real: el correo debe aparecer en una página con cita verificada. No
    toca bases ya aprobadas, autorizaciones de formulario (las revisa el administrador) ni
    contactos excluidos o anonimizados. Si el administrador ya registró tipo, texto y prueba,
    se aprueba lo suyo tal cual.
    """
    admin = politica_automatica(c)
    if not admin:
        return False
    ct = obtener(c, "contactos", ident, True)
    if not ct["correo"] or ct["excluido"] or ct["anonimizado_en"]:
        return False
    b = uno(
        c,
        "SELECT * FROM bases_legales WHERE contacto_id=%s AND vigente FOR UPDATE",
        (ident,),
        False,
    )
    if b and (b["aprobada_en"] or b["tipo"] == "autorizacion_formulario"):
        return False
    completa = (
        b
        and b["tipo"] != "pendiente"
        and (b["texto"] or "").strip()
        and (b["prueba"] or "").strip()
    )
    if not completa:
        f = fuente_publicada(c, ct)
        if not f:
            return False
        empresa = obtener(c, "empresas", str(ct["empresa_id"]))["razon_social"]
        fecha = f["obtenido_en"].astimezone(BOGOTA).date().isoformat()
        cita = " ".join((f["cita"] or "").split())[:200]
        legal(
            c,
            ident,
            {
                "tipo": "corporativo_generico",
                "texto": TEXTO_AUTOMATICO.format(empresa=empresa, url=f["url"]),
                "prueba": f"{f['url']} — «{cita}» — obtenido el {fecha}",
            },
        )
    aprobar_legal(c, ident, admin)
    auditar(c, "aprobacion_legal_automatica", "contactos", ident)
    return True


def aplicar_politica(c: Conn) -> int:
    """Aplica la aprobación automática a todos los contactos que la tienen pendiente."""
    filas = todos(
        c,
        """SELECT ct.id FROM contactos ct
           WHERE ct.correo IS NOT NULL AND NOT ct.excluido AND ct.anonimizado_en IS NULL
             AND NOT EXISTS (SELECT 1 FROM bases_legales b
                             WHERE b.contacto_id=ct.id AND b.vigente AND b.aprobada_en IS NOT NULL)""",
    )
    return sum(aprobar_automatica(c, str(f["id"])) for f in filas)


def guardar_cumplimiento(c: Conn, body: Any, admin: str) -> dict[str, Any]:
    """PUT /configuracion/cumplimiento: encender o apagar la aprobación automática."""
    valor = body.get("aprobacionLegalAutomatica") if isinstance(body, dict) else None
    if not isinstance(valor, bool):
        raise HTTPException(400, "Indique si la aprobación automática queda encendida (sí o no)")
    c.execute(
        "UPDATE configuracion SET valor=%s, actualizado_por=%s WHERE clave='cumplimiento'",
        (Jsonb({"aprobacionLegalAutomatica": valor}), admin),
    )
    auditar(
        c,
        "aprobacion_automatica_" + ("encendida" if valor else "apagada"),
        "configuracion",
        "cumplimiento",
    )
    return {"ok": True, "aprobadas": aplicar_politica(c) if valor else 0}


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


def importar_contactos(c: Conn, body: Any) -> dict[str, int]:
    """Importa filas válidas y devuelve cuántas se crearon, omitieron o rechazaron.

    Los duplicados se detectan con dos conjuntos (correos existentes y correos vistos),
    de modo que cada fila se comprueba una sola vez. Un error general, como una empresa
    inexistente, cancela toda la importación.
    """
    if not isinstance(body, dict) or not isinstance(body.get("contactos"), list):
        raise HTTPException(400, "Envíe la lista de contactos a importar")
    filas: list[Any] = body["contactos"]
    if len(filas) > MAX_IMPORTACION:
        raise HTTPException(400, f"Importe como máximo {MAX_IMPORTACION} filas por archivo")
    empresas: set[str] = set()
    for item in filas:
        if not isinstance(item, dict):
            continue
        if (item.get("baseLegal") or {}).get("tipo") == "autorizacion_formulario":
            raise HTTPException(
                400, "La autorización de formulario debe proceder del webhook firmado"
            )
        empresa = str(item.get("empresaId") or "")
        try:
            UUID(empresa)
        except ValueError as exc:
            raise HTTPException(400, "Seleccione una empresa válida para la importación") from exc
        empresas.add(empresa)
    if empresas:
        validas = {
            str(r["id"])
            for r in todos(
                c,
                "SELECT id FROM empresas WHERE id=ANY(%s::uuid[]) AND fusionada_en_id IS NULL",
                (sorted(empresas),),
            )
        }
        if empresas - validas:
            raise HTTPException(404, "Empresa no encontrada")
    correos = sorted(
        {"".join(str(x.get("correo") or "").lower().split()) for x in filas if isinstance(x, dict)}
        - {""}
    )
    existentes = {
        str(r["correo"]).lower()
        for r in todos(
            c,
            "SELECT correo::text AS correo FROM contactos WHERE correo=ANY(%s::citext[])",
            (correos,),
        )
    }
    vistos: set[str] = set()
    resultado = {"creados": 0, "omitidos": 0, "rechazados": 0}
    for item in filas:
        if not isinstance(item, dict):
            resultado["rechazados"] += 1
            continue
        correo = "".join(str(item.get("correo") or "").lower().split())
        nombre = str(item.get("nombre") or "").strip()
        if not nombre or (correo and not CORREO.fullmatch(correo)):
            resultado["rechazados"] += 1
            continue
        if correo and (correo in existentes or correo in vistos):
            resultado["omitidos"] += 1
            continue
        if correo:
            vistos.add(correo)
        try:
            with c.transaction():
                guardar_contacto(c, {**item, "nombre": nombre, "correo": correo or None})
            resultado["creados"] += 1
        except pg_errors.UniqueViolation:
            resultado["omitidos"] += 1
        except (ValidationError, pg_errors.CheckViolation, pg_errors.NotNullViolation):
            resultado["rechazados"] += 1
    return resultado


def descartar_duplicado(c: Conn, ident: str, admin: str) -> None:
    d = obtener(c, "posibles_duplicados", ident, True)
    if d["estado"] != "pendiente":
        raise HTTPException(400, "Duplicado ya resuelto")
    actualizar(
        c,
        "posibles_duplicados",
        ident,
        {"estado": "descartado", "resuelto_por": admin, "resuelto_en": ahora()},
    )


def fusionar(c: Conn, ident: str, body: Row, admin: str) -> None:
    d = obtener(c, "posibles_duplicados", ident, True)
    if d["estado"] != "pendiente":
        raise HTTPException(400, "Duplicado ya resuelto")
    a = obtener(c, "empresas", str(d["principal_id"]), True)
    b = obtener(c, "empresas", str(d["duplicado_id"]), True)
    if a["fusionada_en_id"] or b["fusionada_en_id"]:
        raise HTTPException(400, "Una de las empresas ya fue fusionada; descarte este duplicado")
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
    elegidos = {campos[k]: b[campos[k]] for k, v in body.items() if v == "duplicado"}
    if "sitio_web" in elegidos and "dominio" not in elegidos:
        elegidos["dominio"] = dominio(elegidos["sitio_web"]) if elegidos["sitio_web"] else None
    actualizar(c, "empresas", str(a["id"]), elegidos)
    for tabla in ("contactos", "oportunidades", "senales", "evidencias"):
        c.execute(f"UPDATE {tabla} SET empresa_id=%s WHERE empresa_id=%s", (a["id"], b["id"]))
    # Referencias que seguirían apuntando a la ficha fusionada.
    c.execute(
        "UPDATE resultados_descubrimiento SET posible_duplicado_de=%s WHERE posible_duplicado_de=%s",
        (a["id"], b["id"]),
    )
    c.execute(
        "UPDATE resultados_descubrimiento SET empresa_creada_id=%s WHERE empresa_creada_id=%s",
        (a["id"], b["id"]),
    )
    c.execute(
        "UPDATE propuestas SET objetivo_empresa_id=%s WHERE objetivo_empresa_id=%s",
        (a["id"], b["id"]),
    )
    c.execute(
        """UPDATE posibles_duplicados SET estado='descartado',resuelto_por=%s,resuelto_en=now()
        WHERE estado='pendiente' AND id<>%s AND %s IN (principal_id,duplicado_id)""",
        (admin, ident, b["id"]),
    )
    encolar(c, "calcular_puntaje", {"empresaId": str(a["id"])})
    encolar(c, "buscar_duplicados", {"empresaId": str(a["id"])})
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
