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


def duplicados(c: Conn, ident: str | None = None) -> None:
    empresas = todos(c, "SELECT * FROM empresas WHERE fusionada_en_id IS NULL ORDER BY id")
    for i, a in enumerate(empresas):
        for b in empresas[i + 1 :]:
            if ident and ident not in (str(a["id"]), str(b["id"])):
                continue
            if normalizar_nombre(a["municipio"]) != normalizar_nombre(b["municipio"]):
                continue
            similitud = (
                ratio(normalizar_nombre(a["razon_social"]), normalizar_nombre(b["razon_social"]))
                / 100
            )
            if similitud >= 0.88:
                c.execute(
                    "INSERT INTO posibles_duplicados(principal_id,duplicado_id,motivo,similitud) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (a["id"], b["id"], "Nombre similar en el mismo municipio", similitud),
                )
