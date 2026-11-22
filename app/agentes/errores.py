"""Errores de los proveedores de IA con datos para el presupuesto y los reintentos."""

from typing import Any


class FalloProveedor(ValueError):
    """El proveedor no completó la ejecución.

    - ``uso``: tokens y consultas de las respuestas que sí se recibieron (consumo conocido).
    - ``costo_conocido``: False cuando no hubo respuesta (red o tiempo de espera): el cobro
      es incierto y se conserva la reserva hasta conciliarla.
    - ``reintentable``: si un intento posterior puede funcionar (límite por minuto,
      saturación). Una clave inválida o una cuota diaria agotada no lo son.

    El mensaje se arma siempre con textos fijos de este backend: nunca incluye la clave,
    el cuerpo de la respuesta del proveedor ni datos de páginas web.
    """

    def __init__(
        self,
        mensaje: str,
        uso: dict[str, Any] | None = None,
        *,
        costo_conocido: bool = True,
        reintentable: bool = True,
    ) -> None:
        super().__init__(mensaje)
        self.uso = dict(uso or {})
        self.costo_conocido = costo_conocido
        self.reintentable = reintentable
