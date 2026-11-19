from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: Literal["development", "production"] = "production"
    app_url: str
    database_url: SecretStr
    clave_cifrado: SecretStr
    sesion_secreto: SecretStr
    admin_correo: str
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    google_dominio_permitido: str = "nexusinnovacion.com"
    gmail_cuenta_envio: str = ""
    auth_dev_bypass: bool = False
    agente_proveedor: Literal["claude", "gemini"] = "claude"
    gemini_api_key: SecretStr = SecretStr("")
    gemini_modelo: str = "gemini-3.5-flash-lite"
    gemini_precios_json: str = '{"gemini-3.5-flash-lite":{"entrada":0.30,"salida":2.50}}'
    gemini_costo_busqueda_usd: float = Field(default=0.014, ge=0)
    descubrimiento_automatico: bool = False
    descubrimiento_limite_diario: int = Field(default=20, ge=1, le=100)
    descubrimiento_hora: int = Field(default=8, ge=0, le=23)
    anthropic_api_key: SecretStr = SecretStr("")
    claude_modelo_ligero: str = "claude-haiku-5-5"
    claude_modelo_mayor: str = "claude-sonnet-5-5"
    claude_precios_json: str = "{}"
    costo_busqueda_web_usd: float = 0.01
    tope_usd_por_tarea: float = 0.50
    tope_mensual_usd: float = 30
    hunter_api_key: SecretStr = SecretStr("")
    google_cuenta_servicio_json: SecretStr = SecretStr("")
    ga4_propiedad_id: str = ""
    search_console_sitio: str = ""
    formulario_webhook_secreto: SecretStr = SecretStr("")
    diagnostico_url: str = "https://nexusinnovacion.com/diagnostico"
    tasa_cop_por_usd: float = 4000
    umbral_calificado: int = 70

    @model_validator(mode="after")
    def validar(self) -> Self:
        Fernet(self.clave_cifrado.get_secret_value().encode())
        if len(self.sesion_secreto.get_secret_value()) < 32:
            raise ValueError("SESION_SECRETO requiere al menos 32 caracteres")
        self.app_url = self.app_url.rstrip("/")
        u = urlsplit(self.app_url)
        if u.path or u.query or u.fragment or u.scheme not in ("http", "https"):
            raise ValueError("APP_URL debe ser un origen sin ruta")
        if self.app_env == "production":
            if self.auth_dev_bypass or u.scheme != "https":
                raise ValueError("Producción exige HTTPS y prohíbe AUTH_DEV_BYPASS")
            if not self.google_client_id or not self.google_client_secret.get_secret_value():
                raise ValueError("Configure OAuth de Google para producción")
        if self.tasa_cop_por_usd <= 0:
            raise ValueError("TASA_COP_POR_USD debe ser positiva")
        return self


@lru_cache
def config() -> Config:
    return Config()  # type: ignore[call-arg]
