from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID


def camel(k: str) -> str:
    especiales = {"costo_usd": "costoUSD", "valor_estimado_cop": "valorEstimadoCOP"}
    if k in especiales:
        return especiales[k]
    a, *b = k.split("_")
    return a + "".join(x.capitalize() for x in b)


def serializar(v: Any) -> Any:
    if isinstance(v, dict):
        return {
            camel(k): serializar(x)
            for k, x in v.items()
            if x is not None or k in ("valor", "cumple", "costoUSD")
        }
    if isinstance(v, (list, tuple)):
        return [serializar(x) for x in v]
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    return v
