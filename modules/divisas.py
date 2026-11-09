"""
DIVISAS - Tipo de cambio de hoy según el Banco Central Europeo.

Usa Frankfurter (referencias diarias del BCE): gratis y sin clave.
En el simulador sirve para rellenar el tipo de cambio del Paso 5.

El BCE publica en días hábiles hacia las 16:00 CET: en fin de semana,
festivo o antes de esa hora devuelve la última referencia publicada.
"""
from __future__ import annotations

import requests

BASE = "https://api.frankfurter.dev/v1"


def usd_por_euro() -> float | None:
    """Dólares por 1 € según la última referencia del BCE.
    Devuelve None si la consulta falla."""
    try:
        r = requests.get(f"{BASE}/latest",
                         params={"base": "EUR", "symbols": "USD"}, timeout=20)
        r.raise_for_status()
        return r.json()["rates"]["USD"]
    except Exception:
        return None
