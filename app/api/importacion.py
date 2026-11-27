"""Importación de contactos desde .csv o .xlsx y exportación a .xlsx.

Flujo de importación (nada se escribe antes de confirmar):
1. POST /contactos/importacion/analizar: el archivo (base64) se lee en el servidor; devuelve
   columnas, filas con su número real de fila y un mapeo sugerido. Límites: 4 MB, 20.000
   filas y 60 columnas; las filas vacías se cuentan y se ignoran.
2. POST /contactos/importacion/previsualizar: con el mapeo y las opciones, valida cada fila
   y dice qué pasará (crear, actualizar, omitir o error, con el motivo). No escribe.
3. POST /contactos/importacion/confirmar: aplica hasta 500 filas por petición (el frontend
   envía lotes). Cada fila va en su propio punto de guardado: si una falla, no deja una
   empresa ni una asociación a medias, y las demás siguen.

Reglas: correos en minúsculas y sin espacios, nombres y tildes normalizados (NFC), teléfonos
siempre como texto, correo repetido dentro del archivo (gana la primera fila) o ya existente
(omitir o actualizar, a elección). Al actualizar, una celda vacía conserva el dato salvo que
se elija «borrar vacíos». Un correo dado de baja nunca se importa ni se reactiva.
"""

import base64
import binascii
import csv
import io
import re
import unicodedata
from datetime import date, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from psycopg import errors as pg_errors
from pydantic import ValidationError

from app.api import datos
from app.db import Conn, Row, obtener, todos
from app.dominio.reglas import dominio as dominio_de
from app.dominio.reglas import normalizar_nombre

MAX_BYTES = 4_000_000
MAX_FILAS = 20_000
MAX_COLUMNAS = 60
LOTE = 500
CORREO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GRATUITOS = {"gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "live.com", "icloud.com"}
CAMPOS = (
    "nombre",
    "correo",
    "cargo",
    "telefono",
    "empresa",
    "nit",
    "municipio",
    "sector",
    "sitioWeb",
)
SINONIMOS = {
    "nombre": ("nombre", "nombres", "nombre completo", "name", "full name", "contacto"),
    "correo": ("correo", "email", "e-mail", "correo electronico", "mail", "correo-e"),
    "cargo": ("cargo", "puesto", "position", "title", "rol"),
    "telefono": ("telefono", "celular", "movil", "phone", "whatsapp", "tel"),
    "empresa": ("empresa", "razon social", "compania", "company", "organizacion"),
    "nit": ("nit", "rut", "identificacion tributaria"),
    "municipio": ("municipio", "ciudad", "city", "localidad"),
    "sector": ("sector", "industria", "industry"),
    "sitioWeb": ("sitio web", "web", "website", "pagina web", "url", "sitio"),
}


def _sin_tildes(s: str) -> str:
    return "".join(
        x for x in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(x)
    ).strip()


def texto(v: Any) -> str:
    """Celda → texto. Los números enteros pierden el «.0» (teléfonos, NIT)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "sí" if v else "no"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return unicodedata.normalize("NFC", str(v)).strip()


def _decodificar(contenido: Any) -> bytes:
    if not isinstance(contenido, str) or not contenido:
        raise HTTPException(400, "Adjunte un archivo .csv o .xlsx")
    if "," in contenido[:100] and contenido.startswith("data:"):
        contenido = contenido.split(",", 1)[1]
    try:
        raw = base64.b64decode(contenido, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "El archivo no llegó completo; vuelva a elegirlo") from exc
    if len(raw) > MAX_BYTES:
        raise HTTPException(
            413, f"El archivo supera {MAX_BYTES // 1_000_000} MB: divídalo en partes más pequeñas"
        )
    return raw


def _leer_csv(raw: bytes) -> list[list[str]]:
    for cod in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            txt = raw.decode(cod)
            break
        except UnicodeDecodeError:
            continue
    muestra = txt[:5000]
    try:
        dialecto = csv.Sniffer().sniff(muestra, delimiters=",;\t|")
        sep = dialecto.delimiter
    except csv.Error:
        sep = ";" if muestra.count(";") > muestra.count(",") else ","
    return [[texto(x) for x in fila] for fila in csv.reader(io.StringIO(txt), delimiter=sep)]


def _leer_xlsx(raw: bytes, hoja: str | None) -> tuple[list[list[str]], list[str], str]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # archivo dañado o que no es xlsx
        raise HTTPException(400, "No se pudo leer el archivo como Excel (.xlsx)") from exc
    hojas = list(wb.sheetnames)
    nombre = hoja if hoja and hoja in hojas else hojas[0]
    filas = []
    for fila in wb[nombre].iter_rows(values_only=True):
        filas.append([texto(x) for x in fila[:MAX_COLUMNAS]])
        if len(filas) > MAX_FILAS + 1:
            break
    wb.close()
    return filas, hojas, nombre


def analizar(body: Any) -> dict[str, Any]:
    body = body if isinstance(body, dict) else {}
    nombre = str(body.get("nombreArchivo") or "")
    raw = _decodificar(body.get("contenido"))
    hojas: list[str] = []
    hoja = ""
    if nombre.lower().endswith(".xlsx") or raw[:2] == b"PK":
        matriz, hojas, hoja = _leer_xlsx(raw, body.get("hoja"))
    elif nombre.lower().endswith((".csv", ".txt")) or not nombre:
        matriz = _leer_csv(raw)
    else:
        raise HTTPException(400, "Formato no admitido: use .csv o .xlsx (no .xls antiguo)")
    # Encabezado: la primera fila con algún valor.
    inicio = next((i for i, f in enumerate(matriz) if any(f)), None)
    if inicio is None:
        raise HTTPException(400, "El archivo no tiene filas con datos")
    encabezado = matriz[inicio]
    ancho = max(len(encabezado), *(len(f) for f in matriz[inicio + 1 :])) if matriz else 0
    if ancho > MAX_COLUMNAS:
        raise HTTPException(400, f"El archivo tiene más de {MAX_COLUMNAS} columnas")
    columnas = []
    for i in range(ancho):
        h = encabezado[i] if i < len(encabezado) else ""
        columnas.append(h or f"Columna {i + 1}")
    filas = []
    vacias = 0
    for n, f in enumerate(matriz[inicio + 1 :], start=inicio + 2):
        if not any(x.strip() for x in f):
            vacias += 1
            continue
        filas.append({"fila": n, "valores": f + [""] * (ancho - len(f))})
    if len(filas) > MAX_FILAS:
        raise HTTPException(
            413, f"El archivo tiene más de {MAX_FILAS} filas con datos: divídalo en partes"
        )
    sugerido: dict[str, int] = {}
    for i, h in enumerate(columnas):
        clave = _sin_tildes(h)
        for campo, nombres in SINONIMOS.items():
            if campo not in sugerido and clave in nombres:
                sugerido[campo] = i
    return {
        "columnas": columnas,
        "filas": filas,
        "totalFilas": len(filas),
        "filasVacias": vacias,
        "mapeoSugerido": sugerido,
        "hojas": hojas,
        "hoja": hoja,
        "limites": {"bytes": MAX_BYTES, "filas": MAX_FILAS, "lote": LOTE},
    }


# ---------------------------------------------------------------------- planificación


def _opciones(c: Conn, body: Any) -> Row:
    o = (body or {}).get("opciones") or {}
    existentes = o.get("existentes", "omitir")
    vacios = o.get("vacios", "conservar")
    if existentes not in ("omitir", "actualizar") or vacios not in ("conservar", "borrar"):
        raise HTTPException(400, "Opciones de importación no válidas")
    lista_id = o.get("listaId") or None
    if lista_id:
        lst = obtener(c, "listas", str(lista_id))
        if lst["archivada_en"] or lst["tipo"] != "estatica":
            raise HTTPException(400, "Elija una lista estática activa para asignar los contactos")
    empresa_id = o.get("empresaId") or None
    if empresa_id:
        try:
            UUID(str(empresa_id))
        except ValueError as exc:
            raise HTTPException(400, "Empresa por defecto no válida") from exc
        e = obtener(c, "empresas", str(empresa_id))
        if e["fusionada_en_id"]:
            raise HTTPException(400, "La empresa por defecto fue fusionada con otra")
    legal = o.get("baseLegal") or {"tipo": "pendiente"}
    if legal.get("tipo") == "autorizacion_formulario":
        raise HTTPException(400, "La autorización de formulario debe proceder del webhook firmado")
    return {
        "existentes": existentes,
        "vacios": vacios,
        "listaId": lista_id,
        "empresaId": empresa_id,
        "crearEmpresas": o.get("crearEmpresas", True) is not False,
        "origen": o.get("origen") or "manual",
        "baseLegal": legal,
    }


def _filas(body: Any, maximo: int) -> list[Row]:
    filas = (body or {}).get("filas")
    if not isinstance(filas, list) or not filas:
        raise HTTPException(400, "No hay filas para importar")
    if len(filas) > maximo:
        raise HTTPException(413, f"Envíe como máximo {maximo} filas por petición")
    out = []
    for f in filas:
        if not isinstance(f, dict) or not isinstance(f.get("datos"), dict):
            raise HTTPException(400, "Formato de filas no válido")
        d = {k: texto(f["datos"].get(k)) for k in CAMPOS}
        d["nombre"] = " ".join(d["nombre"].split())
        d["correo"] = "".join(d["correo"].lower().split())
        out.append({"fila": int(f.get("fila") or 0), "datos": d})
    return out


class _Indice:
    """Empresas y contactos existentes, cargados una vez por petición."""

    def __init__(self, c: Conn, correos: list[str]) -> None:
        self.por_nit: dict[str, Row] = {}
        self.por_dominio: dict[str, Row] = {}
        self.por_nombre: dict[str, list[Row]] = {}
        for e in todos(
            c,
            "SELECT id,razon_social,nit,dominio::text AS dominio,municipio FROM empresas "
            "WHERE fusionada_en_id IS NULL",
        ):
            if e["nit"]:
                self.por_nit[e["nit"].strip()] = e
            if e["dominio"]:
                self.por_dominio[e["dominio"].lower()] = e
            self.por_nombre.setdefault(normalizar_nombre(e["razon_social"]), []).append(e)
        self.contactos = {
            str(r["correo"]).lower(): r
            for r in todos(
                c,
                "SELECT id,correo::text AS correo,empresa_id,excluido FROM contactos "
                "WHERE correo=ANY(%s::citext[])",
                (correos,),
            )
        }
        self.suprimidos = {
            str(r["correo"]).lower()
            for r in todos(
                c,
                """SELECT x.correo FROM unnest(%s::text[]) AS x(correo)
                   WHERE EXISTS (SELECT 1 FROM supresiones s WHERE s.correo=x.correo::citext
                     OR s.dominio=split_part(x.correo,'@',2)::citext)""",
                (correos,),
            )
        }
        self.nuevas: dict[str, str] = {}  # empresas a crear en este archivo (por nombre)

    def empresa(self, d: Row) -> Row | None:
        if d["nit"] and d["nit"] in self.por_nit:
            return self.por_nit[d["nit"]]
        dom = dominio_de(d["sitioWeb"]) if d["sitioWeb"] else ""
        if not dom and "@" in d["correo"]:
            dom = d["correo"].split("@")[1]
            if dom in GRATUITOS:
                dom = ""
        if dom and dom in self.por_dominio and d["empresa"]:
            return self.por_dominio[dom]
        if d["empresa"]:
            candidatas = self.por_nombre.get(normalizar_nombre(d["empresa"]), [])
            if d["municipio"]:
                mismas = [
                    e
                    for e in candidatas
                    if _sin_tildes(e["municipio"]) == _sin_tildes(d["municipio"])
                ]
                candidatas = mismas or candidatas
            if candidatas:
                return candidatas[0]
        return None


def planear(c: Conn, filas: list[Row], op: Row) -> tuple[list[Row], _Indice]:
    correos = sorted({f["datos"]["correo"] for f in filas if f["datos"]["correo"]})
    idx = _Indice(c, correos)
    vistos: dict[str, int] = {}
    plan = []
    for f in filas:
        d = f["datos"]
        item: Row = {
            "fila": f["fila"],
            "nombre": d["nombre"],
            "correo": d["correo"],
            "empresa": d["empresa"],
            "datos": d,
        }

        def poner(accion: str, motivo: str = "", item: Row = item) -> None:
            item["accion"], item["motivo"] = accion, motivo
            plan.append(item)

        if not d["nombre"]:
            poner("error", "Falta el nombre")
            continue
        if d["correo"] and not CORREO.fullmatch(d["correo"]):
            poner("error", "Correo con formato no válido")
            continue
        if not d["correo"]:
            poner("error", "Falta el correo: sin él no se puede evitar duplicados ni escribir")
            continue
        if d["correo"] in vistos:
            poner(
                "omitir",
                f"Correo repetido en el archivo (ya está en la fila {vistos[d['correo']]})",
            )
            continue
        vistos[d["correo"]] = f["fila"]
        if d["correo"] in idx.suprimidos:
            poner("omitir", "Correo dado de baja: no se importa ni se reactiva")
            continue
        existente = idx.contactos.get(d["correo"])
        if existente:
            item["contactoId"] = str(existente["id"])
            if op["existentes"] == "omitir":
                poner("omitir", "Ya existe un contacto con ese correo")
            else:
                poner(
                    "actualizar",
                    "Se actualizan los campos con valor"
                    if op["vacios"] == "conservar"
                    else "Se actualizan todos los campos mapeados (los vacíos se borran)",
                )
            continue
        e = idx.empresa(d)
        if e:
            item["empresaId"] = str(e["id"])
            item["empresaNombre"] = e["razon_social"]
            poner("crear", f"Empresa existente: {e['razon_social']}")
        elif d["empresa"] and op["crearEmpresas"]:
            item["empresaNueva"] = d["empresa"]
            poner("crear", f"Se creará la empresa «{d['empresa']}»")
        elif op["empresaId"]:
            item["empresaId"] = op["empresaId"]
            poner("crear", "Empresa por defecto")
        else:
            poner("error", "Sin empresa: mapee la columna de empresa o elija una por defecto")
    return plan, idx


def _resumen(plan: list[Row]) -> dict[str, int]:
    r = {"crear": 0, "actualizar": 0, "omitir": 0, "error": 0}
    for x in plan:
        r[x["accion"]] += 1
    return r


def previsualizar(c: Conn, body: Any) -> dict[str, Any]:
    op = _opciones(c, body)
    plan, _ = planear(c, _filas(body, MAX_FILAS), op)
    return {
        "resumen": _resumen(plan),
        "filas": [{k: v for k, v in x.items() if k != "datos"} for x in plan],
    }


def confirmar(c: Conn, body: Any) -> dict[str, Any]:
    from app.api.listas import agregar_ids

    op = _opciones(c, body)
    plan, idx = planear(c, _filas(body, LOTE), op)
    res = {"creados": 0, "actualizados": 0, "omitidos": 0, "fallidos": 0}
    detalle: list[Row] = []
    para_lista: list[str] = []
    for x in plan:
        d = x["datos"]
        if x["accion"] in ("omitir", "error"):
            res["omitidos" if x["accion"] == "omitir" else "fallidos"] += 1
            detalle.append({"fila": x["fila"], "resultado": x["accion"], "motivo": x["motivo"]})
            continue
        try:
            with c.transaction():
                if x["accion"] == "actualizar":
                    cuerpo: Row = {}
                    for campo in ("nombre", "cargo", "telefono"):
                        if d[campo] or (op["vacios"] == "borrar" and campo != "nombre"):
                            cuerpo[campo] = d[campo] or None
                    datos.guardar_contacto(c, cuerpo, x["contactoId"])
                    cid = x["contactoId"]
                    res["actualizados"] += 1
                    detalle.append({"fila": x["fila"], "resultado": "actualizado", "motivo": ""})
                else:
                    empresa_id = x.get("empresaId")
                    if not empresa_id:
                        clave = normalizar_nombre(x["empresaNueva"])
                        empresa_id = idx.nuevas.get(clave)
                        if not empresa_id:
                            e = datos.guardar_empresa(
                                c,
                                {
                                    "razonSocial": x["empresaNueva"],
                                    "nit": d["nit"] or None,
                                    "municipio": d["municipio"],
                                    "sector": d["sector"] or "Otro",
                                    "sitioWeb": d["sitioWeb"] or None,
                                },
                            )
                            empresa_id = str(e["id"])
                    r = datos.guardar_contacto(
                        c,
                        {
                            "empresaId": empresa_id,
                            "nombre": d["nombre"],
                            "correo": d["correo"],
                            "cargo": d["cargo"] or None,
                            "telefono": d["telefono"] or None,
                            "origen": op["origen"],
                            "baseLegal": op["baseLegal"],
                        },
                    )
                    if x.get("empresaNueva"):
                        idx.nuevas[normalizar_nombre(x["empresaNueva"])] = empresa_id
                    cid = str(r["id"])
                    res["creados"] += 1
                    detalle.append({"fila": x["fila"], "resultado": "creado", "motivo": ""})
                para_lista.append(cid)
        except pg_errors.UniqueViolation as exc:
            res["fallidos"] += 1
            motivo = {
                "contactos_correo_uq": "Ya existe un contacto con ese correo",
                "empresas_nit_uq": "Ya existe otra empresa con ese NIT",
                "empresas_dominio_uq": "Ya existe otra empresa con ese dominio",
            }.get(exc.diag.constraint_name or "", "Registro duplicado")
            detalle.append({"fila": x["fila"], "resultado": "error", "motivo": motivo})
        except (
            ValidationError,
            HTTPException,
            pg_errors.CheckViolation,
            pg_errors.NotNullViolation,
        ) as exc:
            res["fallidos"] += 1
            motivo = (
                str(exc.detail)
                if isinstance(exc, HTTPException)
                else "Algún dato no cumple las reglas (revise teléfono, correo o tamaño)"
            )
            detalle.append({"fila": x["fila"], "resultado": "error", "motivo": motivo})
    if op["listaId"] and para_lista:
        agregar_ids(c, str(op["listaId"]), para_lista)
    return {**res, "filas": detalle}


# ---------------------------------------------------------------------- exportación

COLUMNAS_EXPORTACION = (
    "Nombre",
    "Correo",
    "Cargo",
    "Teléfono",
    "Empresa",
    "NIT",
    "Municipio",
    "Sector",
    "Etapa",
    "Estado del correo",
    "Base legal",
    "Elegible para envíos",
    "Motivo si no es elegible",
    "Dado de baja",
    "Listas",
    "Origen",
    "Creado",
)
PELIGROSOS = ("=", "@", "\t", "\r")


def celda_segura(v: Any) -> str:
    """Texto que Excel nunca interpreta como fórmula.

    Además de guardarse como texto, se antepone «'» a lo que empieza como fórmula
    (=, @, tabulador, o +/- seguidos de algo que no sea un número de teléfono).
    """
    s = "" if v is None else str(v)
    if s.startswith(PELIGROSOS) or (s[:1] in "+-" and not re.fullmatch(r"[+-][\d\s()-]{5,}", s)):
        return "'" + s
    return s


def ids_exportacion(c: Conn, params: Any) -> tuple[list[str], str]:
    from app.api import lecturas
    from app.motor import audiencia

    ambito = params.get("ambito", "todo")
    if ambito == "lista":
        lst = obtener(c, "listas", str(params.get("listaId")))
        return audiencia.ids_lista(c, lst), "lista-" + normalizar_nombre(lst["nombre"])[:30]
    if ambito == "segmento":
        seg = obtener(c, "segmentos", str(params.get("segmentoId")))
        return lecturas.ids_segmento(c, seg["reglas"]), "segmento-" + normalizar_nombre(
            seg["nombre"]
        )[:30]
    filtros = ["ct.anonimizado_en IS NULL"]
    valores: list[Any] = []
    if ambito == "filtro":
        if params.get("q"):
            filtros.append("(ct.nombre ILIKE %s OR ct.correo::text ILIKE %s)")
            q = "%" + str(params["q"]).replace("%", r"\%").replace("_", r"\_") + "%"
            valores += [q, q]
        for campo, columna in (
            ("etapa", "ct.etapa"),
            ("estadoCorreo", "ct.estado_correo"),
            ("origen", "ct.origen"),
            ("municipio", "e.municipio"),
            ("sector", "e.sector"),
            ("empresaId", "ct.empresa_id::text"),
        ):
            if params.get(campo):
                filtros.append(f"{columna}=%s")
                valores.append(str(params[campo]))
        if params.get("baseLegal") in ("aprobada", "pendiente"):
            filtros.append(
                ("" if params["baseLegal"] == "aprobada" else "NOT ")
                + "coalesce(b.aprobada_por_admin,false)"
            )
        if params.get("excluido") in ("sí", "si", "no"):
            filtros.append(("" if params["excluido"] != "no" else "NOT ") + "ct.excluido")
    elif ambito != "todo":
        raise HTTPException(400, "Ámbito de exportación no válido")
    filas = todos(
        c,
        "SELECT ct.id FROM contactos ct JOIN empresas e ON e.id=ct.empresa_id "
        "LEFT JOIN v_base_legal_vigente b ON b.contacto_id=ct.id WHERE "
        + " AND ".join(filtros)
        + " ORDER BY ct.creado_en, ct.id",
        tuple(valores),
    )
    return [str(r["id"]) for r in filas], ("filtro" if ambito == "filtro" else "todos")


def exportar_xlsx(c: Conn, params: Any) -> tuple[bytes, str]:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    ids, nombre = ids_exportacion(c, params)
    wb = Workbook()
    ws = wb.active
    ws.title = "Contactos"
    ws.append(list(COLUMNAS_EXPORTACION))
    for celda in ws[1]:
        celda.font = Font(bold=True)
    filas = (
        todos(
            c,
            """SELECT ct.nombre, ct.correo::text AS correo, ct.cargo, ct.telefono,
                      e.razon_social, e.nit, e.municipio, e.sector, ct.etapa, ct.estado_correo,
                      coalesce(b.aprobada_por_admin,false) AS legal,
                      motivo_no_elegible(ct.id) AS motivo, contacto_bloqueado(ct.id) AS baja,
                      ct.origen, ct.creado_en,
                      (SELECT string_agg(l.nombre, ', ' ORDER BY l.nombre) FROM lista_miembros lm
                        JOIN listas l ON l.id=lm.lista_id
                        WHERE lm.contacto_id=ct.id AND lm.activo AND l.archivada_en IS NULL) AS listas
               FROM unnest(%s::uuid[]) WITH ORDINALITY AS x(id,n)
               JOIN contactos ct ON ct.id=x.id JOIN empresas e ON e.id=ct.empresa_id
               LEFT JOIN v_base_legal_vigente b ON b.contacto_id=ct.id
               WHERE ct.anonimizado_en IS NULL ORDER BY x.n""",
            (ids,),
        )
        if ids
        else []
    )
    for f in filas:
        valores = [
            f["nombre"],
            f["correo"],
            f["cargo"],
            f["telefono"],
            f["razon_social"],
            f["nit"],
            f["municipio"],
            f["sector"],
            f["etapa"],
            f["estado_correo"],
            "aprobada" if f["legal"] else "pendiente",
            "no" if f["motivo"] else "sí",
            f["motivo"] or "",
            "sí" if f["baja"] else "no",
            f["listas"] or "",
            f["origen"],
            f["creado_en"].date().isoformat() if f["creado_en"] else "",
        ]
        ws.append([celda_segura(v) for v in valores])
    # Todo como texto: teléfonos y NIT conservan ceros y «+», y nada se evalúa como fórmula.
    for fila in ws.iter_rows(min_row=2):
        for celda in fila:
            celda.data_type = "s"
            celda.number_format = "@"
    for i, ancho in enumerate((28, 32, 22, 16, 30, 14, 16, 16, 14, 14, 12, 10, 30, 10, 24, 18, 12)):
        ws.column_dimensions[chr(ord("A") + i)].width = ancho
    ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), f"nexus-contactos-{nombre}-{datos.ahora():%Y%m%d}.xlsx"
