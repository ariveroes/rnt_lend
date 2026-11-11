"""
CÁLCULO DEL PRÉSTAMO EN RNT LEND

Cuánto puede pedir hoy un inversor, con sus inmuebles como garantía, para
invertir en un proyecto y no pagar nada (ni intereses) hasta que ese proyecto
termine.

SUPUESTOS
- La deuda crece con los intereses de forma continua, como en el protocolo:
  D(t) = D0 · e^(r·t). No se paga nada hasta el final.
- La garantía vale lo que dice el oráculo de RNT Lend y ese valor no cambia.
- La deuda no puede pasar del umbral de liquidación de la garantía (80 %): el último
  día, con todos los intereses, el factor de salud llega como mínimo a 1.
- Al pedir el préstamo, además, el protocolo no deja superar el LTV de cada activo.
- Si un inmueble de la garantía termina antes que el proyecto que se compra, desde
  ese día deja de contar como garantía (sus tokens desaparecen).
- Los tokens que se compran con el préstamo no cuentan como garantía (la app pasa
  nuevo["garantia"] = False; la función lo admite por si algún día se quiere).

Todo en dólares (el préstamo es en USDT).
"""
from __future__ import annotations

import math

LIMITE_DEUDA = 0.80             # umbral de liquidación normal de RNT Lend
MARGEN_LIQUIDACION = 0.0


def limite_activo(umbral: float) -> float:
    """Parte del valor de un activo que puede llegar a deberse."""
    if not umbral:
        return LIMITE_DEUDA
    return max(0.0, min(LIMITE_DEUDA, umbral - MARGEN_LIQUIDACION))


def _viva(g, t, final):
    """¿Sigue contando como garantía en el mes t?"""
    fin = g.get("meses_fin")
    if fin is None:
        return True
    return fin >= t if final else fin > t


def capacidad(garantias: list, deuda0: float, apr: float, meses_plazo: float, nuevo: dict,
              meses_pago: float | None = None) -> dict:
    """Préstamo máximo y qué lo limita.

    meses_pago: si se pagan los intereses cada tantos meses, la deuda vuelve a lo
      pedido en cada pago y como mucho crece durante ese tiempo. None = no se paga
      nada hasta el final (lo normal en la herramienta).

    garantias: [{"nombre", "valor_usd", "ltv", "umbral", "meses_fin"}]; meses_fin
      None si no se sabe (se supone que dura más que el plazo).
    nuevo: {"garantia": bool, "ltv", "umbral", "ratio"}; ratio = valor como garantía
      de cada dólar invertido en el proyecto (≈ 1).
    """
    validas = [g for g in garantias if g.get("valor_usd", 0) > 0 and g.get("ltv", 0) > 0]
    vencidas = [g for g in validas if g.get("meses_fin") is not None and g["meses_fin"] <= 0]
    activas = [g for g in validas if g not in vencidas]
    T = max(0.0, meses_plazo)
    usa_nuevo = bool(nuevo.get("garantia"))
    k_ltv = nuevo.get("ratio", 1.0) * nuevo.get("ltv", 0.0) if usa_nuevo else 0.0
    k_lim = nuevo.get("ratio", 1.0) * limite_activo(nuevo.get("umbral", 0.0)) if usa_nuevo else 0.0

    candidatos = []
    # 1) Hoy: lo que deja pedir el protocolo (LTV)
    cap_ltv = sum(g["valor_usd"] * g["ltv"] for g in activas)
    candidatos.append({"maximo": (cap_ltv - deuda0) / max(1e-9, 1 - k_ltv),
                       "motivo": "hoy", "mes": 0.0, "salen": []})
    # 2) Cada vez que termina una garantía antes del plazo, y el último día
    puntos = sorted({g["meses_fin"] for g in activas
                     if g.get("meses_fin") is not None and g["meses_fin"] < T})
    for t in puntos + [T]:
        final = (t == T)
        vivas = [g for g in activas if _viva(g, t, final)]
        c_lim = sum(g["valor_usd"] * limite_activo(g.get("umbral")) for g in vivas)
        crece = t if not meses_pago else min(t, meses_pago)     # lo que puede crecer la deuda sin pagar
        f = math.exp(-apr * crece / 12)
        candidatos.append({
            "maximo": (c_lim * f - deuda0) / max(1e-9, 1 - k_lim * f),
            "motivo": "final" if final else "fin_garantia", "mes": t,
            "salen": [g["nombre"] for g in activas if g not in vivas]})
    limite = min(candidatos, key=lambda c: c["maximo"])
    return {"maximo": max(0.0, limite["maximo"]), "limita": limite, "vencidas": vencidas,
            "activas": activas, "garantia_hoy": sum(g["valor_usd"] for g in activas)}


def evolucion(garantias: list, deuda0: float, prestamo: float, apr: float, meses_plazo: float,
              nuevo: dict, pasos: int = 60) -> list:
    """Deuda sobre garantía (%) a lo largo del plazo, para el gráfico.
    Devuelve [(mes, % deuda/garantía, deuda, garantía)], con un punto antes y otro
    después de cada garantía que termina (para que se vea el salto)."""
    validas = [g for g in garantias if g.get("valor_usd", 0) > 0 and g.get("ltv", 0) > 0
               and not (g.get("meses_fin") is not None and g["meses_fin"] <= 0)]
    T = max(0.0, meses_plazo)
    extra = nuevo.get("ratio", 1.0) * prestamo if nuevo.get("garantia") else 0.0
    d0 = deuda0 + prestamo
    meses = {T * i / pasos for i in range(pasos + 1)}
    for g in validas:
        fin = g.get("meses_fin")
        if fin is not None and 0 < fin < T:
            meses.update({max(0.0, fin - 1e-3), fin})
    puntos = []
    for t in sorted(meses):
        final = abs(t - T) < 1e-9
        garantia = sum(g["valor_usd"] for g in validas if _viva(g, t, final)) + extra
        deuda = d0 * math.exp(apr * t / 12)
        puntos.append((t, (deuda / garantia * 100) if garantia > 0 else None, deuda, garantia))
    return puntos


def deuda_final(deuda0: float, prestamo: float, apr: float, meses_plazo: float) -> float:
    return (deuda0 + prestamo) * math.exp(apr * meses_plazo / 12)


def intereses(prestamo: float, apr: float, meses_plazo: float) -> float:
    """Intereses que genera solo el préstamo nuevo hasta el final."""
    return prestamo * (math.exp(apr * meses_plazo / 12) - 1)


def intereses_pagando(prestamo: float, apr: float, meses_plazo: float, meses_pago: float) -> tuple:
    """(lo que se paga cada vez, intereses totales) pagando los intereses cada meses_pago meses."""
    if prestamo <= 0 or meses_plazo <= 0:
        return 0.0, 0.0
    cuota = prestamo * (math.exp(apr * meses_pago / 12) - 1)
    completos = int(meses_plazo // meses_pago)
    resto = meses_plazo - completos * meses_pago
    total = cuota * completos + prestamo * (math.exp(apr * resto / 12) - 1)
    return cuota, total
