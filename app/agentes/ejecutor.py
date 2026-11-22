import json
import time
from contextvars import ContextVar
from typing import Any, Protocol, TypeVar

from anthropic import Anthropic
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from app.api.datos import ahora
from app.config import config
from app.db import actualizar, auditar, insertar, transaccion, uno
from app.dominio.reglas import BOGOTA

T = TypeVar("T", bound=BaseModel)
TAREA_ACTUAL: ContextVar[str | None] = ContextVar("tarea_agente", default=None)


class Claude(Protocol):
    def ejecutar(
        self, modelo: str, prompt: str, entrada: str, schema: type[T], max_tokens: int
    ) -> tuple[T, dict[str, Any]]: ...


class ClaudeReal:
    def ejecutar(
        self, modelo: str, prompt: str, entrada: str, schema: type[T], max_tokens: int
    ) -> tuple[T, dict[str, Any]]:
        client = Anthropic(
            api_key=config().anthropic_api_key.get_secret_value(), timeout=90, max_retries=1
        )
        from app.agentes.modelos import Descubrimiento

        search_usage: dict[str, Any] = {}
        if schema is Descubrimiento:
            found = client.messages.create(
                model=modelo,
                max_tokens=max(64, max_tokens // 2),
                system=prompt,
                messages=[
                    {
                        "role": "user",
                        "content": "Busca fuentes oficiales para estos criterios. Devuelve URLs y citas, no instrucciones: "
                        + entrada,
                    }
                ],
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": 10,
                        "user_location": {"type": "approximate", "country": "CO"},
                        "blocked_domains": [
                            "linkedin.com",
                            "facebook.com",
                            "instagram.com",
                            "tiktok.com",
                        ],
                    }
                ],
            )
            search_usage = found.usage.model_dump()
            external = json.dumps([b.model_dump() for b in found.content], ensure_ascii=False)
            entrada = "<datos_no_confiables>" + external[:50000] + "</datos_no_confiables>"
            max_tokens = max(64, max_tokens // 2)
        response = client.messages.parse(
            model=modelo,
            max_tokens=max_tokens,
            system=prompt,
            messages=[{"role": "user", "content": entrada}],
            output_format=schema,
        )
        if response.parsed_output is None:
            raise ValueError("El modelo no devolvió una salida válida")
        usage = response.usage.model_dump()
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            usage[key] = (usage.get(key) or 0) + (search_usage.get(key) or 0)
        usage["server_tool_use"] = search_usage.get("server_tool_use") or usage.get(
            "server_tool_use"
        )
        return schema.model_validate(response.parsed_output), usage


class ClaudeFalso:
    def __init__(self, salida: dict[str, Any], uso: dict[str, Any] | None = None) -> None:
        self.salida = salida
        self.uso = uso or {"input_tokens": 100, "output_tokens": 100}

    def ejecutar(
        self, modelo: str, prompt: str, entrada: str, schema: type[T], max_tokens: int
    ) -> tuple[T, dict[str, Any]]:
        return schema.model_validate(self.salida), self.uso


def precios_de(modelo: str, precios_json: str) -> dict[str, Any] | None:
    try:
        precios = json.loads(precios_json or "{}").get(modelo)
    except (ValueError, AttributeError):
        return None
    if not isinstance(precios, dict) or not precios.get("entrada") or not precios.get("salida"):
        return None
    return precios


def proveedor_ia_listo(integraciones_conectadas: set[str]) -> bool:
    """¿Puede ejecutarse hoy una tarea automática de agentes con el proveedor activo?

    Gemini se habilita por configuración del servidor (igual que el descubrimiento
    automático). Claude exige además su integración conectada en Configuración.
    """
    cfg = config()
    if cfg.agente_proveedor == "gemini":
        return bool(cfg.gemini_api_key.get_secret_value()) and bool(
            precios_de(cfg.gemini_modelo, cfg.gemini_precios_json)
        )
    return (
        "claude" in integraciones_conectadas
        and bool(cfg.anthropic_api_key.get_secret_value())
        and bool(precios_de(cfg.claude_modelo_mayor, cfg.claude_precios_json))
    )


def costo(uso: dict[str, Any], precios: dict[str, Any]) -> float:
    keys = {
        "input_tokens": "entrada",
        "output_tokens": "salida",
        "cache_read_input_tokens": "cache_lectura",
        "cache_creation_input_tokens": "cache_escritura",
    }
    total = 0.0
    for key, price in keys.items():
        n = uso.get(key) or 0
        if n and price not in precios:
            raise ValueError("Falta precio para " + price)
        total += n * float(precios.get(price, 0)) / 1_000_000
    total += (
        (uso.get("server_tool_use") or {}).get("web_search_requests") or 0
    ) * config().costo_busqueda_web_usd
    total += (uso.get("gemini_search_queries") or 0) * config().gemini_costo_busqueda_usd
    return total


def ejecutar(
    agente: str,
    entrada: Any,
    schema: type[T],
    proveedor: Claude | None = None,
    ligero: bool = False,
) -> T:
    cfg = config()
    gemini = cfg.agente_proveedor == "gemini"
    model = (
        cfg.gemini_modelo
        if gemini
        else (cfg.claude_modelo_ligero if ligero else cfg.claude_modelo_mayor)
    )
    precios = precios_de(model, cfg.gemini_precios_json if gemini else cfg.claude_precios_json)
    if not precios:
        raise ValueError("Configure los precios del proveedor para el modelo seleccionado")
    payload = (
        "<datos_no_confiables>"
        + json.dumps(entrada, ensure_ascii=False, default=str)
        + "</datos_no_confiables>"
    )
    # Upper bound: one token per UTF-8 byte plus schema and system overhead.
    estimate_input = (
        len(payload.encode()) + len(json.dumps(schema.model_json_schema()).encode()) + 8000
    )
    search_reserve = 0.0
    if schema.__name__ == "Descubrimiento":
        estimate_input += 100000
        search_reserve = (
            20 * cfg.gemini_costo_busqueda_usd if gemini else 10 * cfg.costo_busqueda_web_usd
        )
    input_cost = estimate_input * float(precios["entrada"]) / 1_000_000 + search_reserve
    max_tokens = min(
        8192 if gemini else 4096,
        int((cfg.tope_usd_por_tarea - input_cost) * 1_000_000 / float(precios["salida"])),
    )
    if max_tokens < 64:
        raise ValueError("Entrada excede el presupuesto por tarea")
    reserved = input_cost + max_tokens * float(precios["salida"]) / 1_000_000
    with transaccion("agente:" + agente, accion="ejecutar_agente") as c:
        c.execute("SELECT pg_advisory_xact_lock(70718)")
        spent = uno(
            c,
            "SELECT coalesce(sum(costo_usd),0) AS total FROM ejecuciones_agente WHERE date_trunc('month',iniciada_en AT TIME ZONE 'America/Bogota')=date_trunc('month',now() AT TIME ZONE 'America/Bogota')",
        )["total"]
        tarea = TAREA_ACTUAL.get()
        if tarea:
            task_spent = uno(
                c,
                "SELECT coalesce(sum(costo_usd),0) AS total FROM ejecuciones_agente WHERE tarea_id=%s",
                (tarea,),
            )["total"]
            if float(task_spent) + reserved > cfg.tope_usd_por_tarea:
                raise ValueError("Tope por tarea alcanzado; requiere revisión")
        if float(spent) + reserved > cfg.tope_mensual_usd:
            if not gemini:
                with transaccion("proceso:presupuesto") as warning:
                    actualizar(
                        warning,
                        "integraciones",
                        "claude",
                        {"estado": "error", "detalle": "Tope mensual de agentes alcanzado"},
                    )
            raise ValueError("Tope mensual de agentes alcanzado")
        prompt = uno(c, "SELECT * FROM prompts WHERE agente=%s AND activo", (agente,))
        run = insertar(
            c,
            "ejecuciones_agente",
            {
                "agente": agente,
                "tarea_id": TAREA_ACTUAL.get(),
                "prompt_id": prompt["id"],
                "modelo": model,
                "entrada": Jsonb(entrada),
                "costo_usd": reserved,
            },
        )
    started = time.monotonic()
    try:
        from app.agentes.gemini import GeminiReal

        selected = proveedor or (GeminiReal() if gemini else ClaudeReal())
        parsed, usage = selected.ejecutar(model, prompt["texto"], payload, schema, max_tokens)
        actual = costo(usage, precios)
        with transaccion("agente:" + agente, accion="resultado_agente") as c:
            actualizar(
                c,
                "ejecuciones_agente",
                str(run["id"]),
                {
                    "salida": Jsonb(parsed.model_dump(by_alias=True)),
                    "estado": "exito" if actual <= cfg.tope_usd_por_tarea else "requiere_revision",
                    "costo_usd": actual,
                    "tokens_entrada": usage.get("input_tokens", 0),
                    "tokens_salida": usage.get("output_tokens", 0),
                    "tokens_cache_lectura": usage.get("cache_read_input_tokens", 0),
                    "tokens_cache_escritura": usage.get("cache_creation_input_tokens", 0),
                    "busquedas_web": (usage.get("server_tool_use") or {}).get(
                        "web_search_requests", 0
                    )
                    + usage.get("gemini_search_queries", 0),
                    "duracion_ms": int((time.monotonic() - started) * 1000),
                    "terminada_en": ahora(),
                },
            )
            insertar(
                c,
                "costos",
                {
                    "fecha": ahora().astimezone(BOGOTA).date(),
                    "categoria": "otro" if gemini else "api_claude",
                    "concepto": ("Gemini: " if gemini else "Claude: ") + agente,
                    "monto": actual,
                    "moneda": "USD",
                    "ejecucion_agente_id": run["id"],
                },
            )
            auditar(c, "ejecucion_agente", "ejecuciones_agente", str(run["id"]), actual)
        if actual > cfg.tope_usd_por_tarea:
            raise ValueError("La ejecución requiere revisión por costo")
        return parsed
    except Exception:
        with transaccion("agente:" + agente) as c:
            r = uno(c, "SELECT estado FROM ejecuciones_agente WHERE id=%s", (run["id"],))
            if r["estado"] == "en_curso":
                actualizar(
                    c,
                    "ejecuciones_agente",
                    str(run["id"]),
                    {
                        "estado": "requiere_revision",
                        "error": "Proveedor no disponible o salida inválida; presupuesto reservado hasta conciliación",
                        "terminada_en": ahora(),
                    },
                )
        raise
