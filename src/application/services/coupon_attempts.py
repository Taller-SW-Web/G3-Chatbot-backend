"""Intentos fallidos de cupón por cliente (`SPEC-13` Req. 5).

Cinco intentos fallidos en diez minutos bloquean más pruebas del mismo cliente.
El contador vive en el proceso: con más de una réplica el límite efectivo se
multiplica por el número de réplicas, igual que `InMemoryRateLimiter`; aceptable
para el despliegue de una sola instancia y a mover a un almacén compartido antes
de escalar.
"""
import asyncio
import math
import time
from collections import deque

LIMITE_FALLOS = 5
VENTANA_SEGUNDOS = 10 * 60


class CouponAttempts:
    """Ventana deslizante de fallos por cliente; no cuenta los aciertos."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._fallos: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def blocked(self, customer_id: str) -> int | None:
        """Segundos de espera si el cliente ya agotó sus intentos, o `None`."""
        async with self._lock:
            return self._bloqueado(customer_id, self._clock())

    async def record_failure(self, customer_id: str) -> None:
        async with self._lock:
            ahora = self._clock()
            if self._bloqueado(customer_id, ahora) is None:
                self._podar(customer_id, ahora).append(ahora)

    async def clear(self, customer_id: str) -> None:
        """Un cupón aplicado reinicia el contador del cliente."""
        async with self._lock:
            self._fallos.pop(customer_id, None)

    def reset(self) -> None:
        """Test helper: forget every window."""
        self._fallos.clear()

    def _podar(self, customer_id: str, ahora: float) -> deque[float]:
        bucket = self._fallos.setdefault(customer_id, deque())
        corte = ahora - VENTANA_SEGUNDOS
        while bucket and bucket[0] <= corte:
            bucket.popleft()
        return bucket

    def _bloqueado(self, customer_id: str, ahora: float) -> int | None:
        bucket = self._podar(customer_id, ahora)
        if len(bucket) < LIMITE_FALLOS:
            return None
        return max(1, math.ceil(bucket[0] + VENTANA_SEGUNDOS - ahora))
