from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Entrada(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class Empresa(Entrada):
    razon_social: str = Field(min_length=1)
    nit: str | None = None
    sector: str = "Otro"
    tamano: Literal["1-10", "11-50", "51-200", "200+", "desconocido"] = "desconocido"
    municipio: str
    sitio_web: str | None = None
    dominio: str | None = None


class BaseLegal(Entrada):
    tipo: Literal[
        "autorizacion_formulario", "autorizacion_registrada", "corporativo_generico", "pendiente"
    ] = "pendiente"
    texto: str | None = None
    prueba: str | None = None
    fecha: str | None = None


class Contacto(Entrada):
    empresa_id: str
    nombre: str = Field(min_length=1)
    cargo: str | None = None
    correo: str | None = None
    telefono: str | None = None
    origen: Literal[
        "organico_busqueda",
        "organico_redes",
        "pagado",
        "correo",
        "referencia",
        "directo_desconocido",
        "manual",
        "registro",
        "descubrimiento",
    ] = "manual"
    base_legal: BaseLegal = Field(default_factory=BaseLegal)


class Ventana(Entrada):
    dias: list[int] = Field(min_length=1)
    desde: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    hasta: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class Variable(Entrada):
    nombre: str
    campo_fuente: str
    respaldo: str


class Paso(Entrada):
    id: str = "nuevo"
    orden: int = Field(ge=1)
    asunto: str | None = None
    cuerpo: str
    espera_dias_habiles: int
    mismo_hilo: bool
    variables: list[Variable] = Field(default_factory=list)


class Secuencia(Entrada):
    nombre: str
    objetivo: str
    llamada_accion: str
    pasos: list[Paso]
    metricas_exito: list[str]
    ventana_envio: Ventana


class Marca(Entrada):
    nombre: str
    oferta: str
    tono: str
    firma: str
    prohibidas: list[str]
    politica: str
    baja: str


class Limites(Entrada):
    tope_diario_nuevos: int = Field(ge=1, le=500)
    separacion_segundos: tuple[int, int]
    ventana: Ventana
    tope_por_dominio: int = Field(ge=1)
    umbral_rebote_pausa: float = Field(ge=0, le=100)
