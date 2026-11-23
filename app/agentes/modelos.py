from typing import Literal

from pydantic import BaseModel, Field

from app.esquemas.entradas import Secuencia


class Dato(BaseModel):
    campo: str
    valor: str | None
    url: str
    cita: str


class Enriquecimiento(BaseModel):
    resumen: str
    datos: list[Dato]


class EmpresaDescubierta(BaseModel):
    razonSocial: str
    sitioWeb: str
    sector: str | None
    municipio: str | None
    correoCorporativo: str | None
    datos: list[Dato]


class Descubrimiento(BaseModel):
    empresas: list[EmpresaDescubierta]


class NotaPaso(BaseModel):
    orden: int
    proposito: str  # p. ej. "Presentar el problema y el diagnóstico gratuito"
    porQue: str  # por qué está escrito así para este segmento


class NotasAgente(BaseModel):
    general: str  # por qué esta ruta encaja con el segmento
    pasos: list[NotaPaso]


class CampanaPropuesta(BaseModel):
    nombre: str
    resumen: str
    secuencia: Secuencia
    notas: NotasAgente


class CorreoReescrito(BaseModel):
    """«Mejorar con IA»: un solo correo reescrito con la instrucción del administrador."""

    asunto: str | None = None  # solo si es el primero o abre un hilo nuevo
    cuerpo: str
    porQue: str  # una frase sobre qué cambió


class Clasificacion(BaseModel):
    clasificacion: Literal[
        "interesado",
        "pide_informacion",
        "quiere_reunion",
        "objecion",
        "ahora_no",
        "no_interesado",
        "baja",
        "fuera_oficina",
        "remite_a_otra_persona",
        "necesita_revision",
    ]
    confianza: float = Field(ge=0, le=1)
    borrador: str
    nombres: list[str]
    fechas: list[str]


class SegmentoPropuesto(BaseModel):
    nombre: str
    justificacion: str
    reglas: list[dict[str, str]]


class RecomendacionPropuesta(BaseModel):
    texto: str
    sustento: dict[str, str | float | int]


class Informe(BaseModel):
    recomendaciones: list[RecomendacionPropuesta]
