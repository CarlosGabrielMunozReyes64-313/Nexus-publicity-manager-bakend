from psycopg.types.json import Jsonb
from rapidfuzz.fuzz import ratio

from app.api.datos import avanzar
from app.config import config
from app.db import Conn, Row, insertar, obtener, todos, uno
from app.dominio.reglas import normalizar_nombre

VALLE = {
    "cali",
    "candelaria",
    "palmira",
    "yumbo",
    "jamundi",
    "buga",
    "tulua",
    "buenaventura",
    "cartago",
    "pradera",
    "florida",
    "el cerrito",
    "guacari",
    "ginebra",
    "d agua",
    "dagua",
    "andalucia",
    "ans ermanuevo",
    "ansermanuevo",
    "argelia",
    "bolivar",
    "caicedonia",
    "calima",
    "el aguila",
    "el cairo",
    "el dovio",
    "la cumbre",
    "la union",
    "la victoria",
    "obando",
    "restrepo",
    "riofrio",
    "roldanillo",
    "san pedro",
    "sevilla",
    "toro",
    "trujillo",
    "ulloa",
    "versalles",
    "vijes",
    "yotoco",
    "zarzal",
    "alcala",
}


def calcular(c: Conn, ident: str) -> None:
    e = obtener(c, "empresas", ident)
    cfg = uno(c, "SELECT * FROM configuracion WHERE clave='cliente_ideal'")
    senales = todos(c, "SELECT * FROM senales WHERE empresa_id=%s", (ident,))
    criterios: list[Row] = []
    valores: list[tuple[str, int, bool | None, str]] = [
        (
            "MiPyme de 11 a 200 empleados",
            40,
            None if e["tamano"] == "desconocido" else e["tamano"] in ("11-50", "51-200"),
            "Tamaño registrado",
        ),
        (
            "Municipio del Valle del Cauca",
            30,
            None
            if not e["municipio"]
            else normalizar_nombre(e["municipio"]) in {normalizar_nombre(x) for x in VALLE},
            "Municipio registrado",
        ),
        (
            "Proveedor de empresas grandes",
            30,
            True if any("proveedor" in s["tipo"].lower() for s in senales) else None,
            "Señal explícita de proveedor; ausencia no significa falso",
        ),
    ]
    for nombre, peso, cumple, explicacion in valores:
        criterios.append(
            {"nombre": nombre, "peso": peso, "cumple": cumple, "explicacion": explicacion}
        )
    total = sum(x["peso"] for x in criterios if x["cumple"] is True)
    incompleto = any(x["cumple"] is None for x in criterios)
    previo = uno(
        c,
        "SELECT total,incompleto,criterios,version_reglas FROM puntajes WHERE empresa_id=%s ORDER BY calculado_en DESC LIMIT 1",
        (ident,),
        False,
    )
    # El cálculo diario no repite una fila idéntica a la vigente.
    if not previo or (
        previo["total"],
        previo["incompleto"],
        previo["criterios"],
        previo["version_reglas"],
    ) != (total, incompleto, criterios, cfg["version"]):
        insertar(
            c,
            "puntajes",
            {
                "empresa_id": ident,
                "total": total,
                "incompleto": incompleto,
                "criterios": Jsonb(criterios),
                "version_reglas": cfg["version"],
            },
        )
    if not incompleto and total >= config().umbral_calificado:
        for ct in todos(
            c, "SELECT id FROM contactos WHERE empresa_id=%s AND etapa='lead'", (ident,)
        ):
            avanzar(c, ct["id"], "calificado", "calificado_por_puntaje")


PAGINA_PUNTAJES = 200


def calcular_lote(tarea_id: str) -> None:
    """Puntaje diario de todas las empresas, por páginas y con punto de control.

    Cada página va en su propia transacción; si el trabajador se interrumpe, el
    reintento continúa desde la última empresa procesada.
    """
    from app.db import transaccion

    while True:
        with transaccion("proceso:calidad", accion="calcular_puntajes") as c:
            tarea = obtener(c, "tareas", tarea_id, True)
            if tarea["datos"].get("_aplicada"):
                return
            desde = tarea["datos"].get("ultimoId") or "00000000-0000-0000-0000-000000000000"
            filas = todos(
                c,
                "SELECT id FROM empresas WHERE fusionada_en_id IS NULL AND id>%s::uuid ORDER BY id LIMIT %s",
                (desde, PAGINA_PUNTAJES),
            )
            for e in filas:
                calcular(c, str(e["id"]))
            if len(filas) < PAGINA_PUNTAJES:
                c.execute(
                    "UPDATE tareas SET datos=datos||'{\"_aplicada\":true}'::jsonb WHERE id=%s",
                    (tarea_id,),
                )
                return
            c.execute(
                "UPDATE tareas SET datos=jsonb_set(datos,'{ultimoId}',to_jsonb(%s::text)) WHERE id=%s",
                (str(filas[-1]["id"]), tarea_id),
            )


def _par(c: Conn, a: Row, b: Row) -> None:
    if str(a["id"]) > str(b["id"]):
        a, b = b, a
    similitud = (
        ratio(normalizar_nombre(a["razon_social"]), normalizar_nombre(b["razon_social"])) / 100
    )
    if similitud >= 0.88:
        c.execute(
            "INSERT INTO posibles_duplicados(principal_id,duplicado_id,motivo,similitud) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (a["id"], b["id"], "Nombre similar en el mismo municipio", similitud),
        )


def duplicados(c: Conn, ident: str | None = None) -> None:
    """Pares con nombre similar en el mismo municipio.

    Con una empresa concreta compara solo esa ficha (O(n)); el recorrido diario agrupa
    por municipio antes de comparar nombres.
    """
    empresas = todos(c, "SELECT * FROM empresas WHERE fusionada_en_id IS NULL ORDER BY id")
    if ident:
        actual = next((e for e in empresas if str(e["id"]) == str(ident)), None)
        if not actual:
            return
        municipio = normalizar_nombre(actual["municipio"])
        for otra in empresas:
            if otra["id"] != actual["id"] and normalizar_nombre(otra["municipio"]) == municipio:
                _par(c, actual, otra)
        return
    grupos: dict[str, list[Row]] = {}
    for e in empresas:
        grupos.setdefault(normalizar_nombre(e["municipio"]), []).append(e)
    for grupo in grupos.values():
        for i, a in enumerate(grupo):
            for b in grupo[i + 1 :]:
                _par(c, a, b)
