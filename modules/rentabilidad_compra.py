"""
RENTABILIDAD_COMPRA - Rentabilidad de un proyecto según el PRECIO y el MOMENTO de compra.

Replica las fórmulas de la plantilla de propuestas (pestaña "Cálculos",
columnas U a AL):

  renta      = renta recurrente anual REAL si el proyecto ya paga; si no, la estimada
  plusvalía  = plusvalía REAL si el proyecto está cerrado; si no, la estimada
  renta ajustada     = renta × (precio emisión / precio compra)
  plusvalía ajustada = (precio emisión × (1 + plusvalía) − precio compra) / precio compra
  total              = renta ajustada / 12 × meses de renta pendientes + plusvalía ajustada
  anualizada         = total × 12 / meses hasta el fin (o de renta pendientes,
                       si el Master no tiene los meses hasta el fin)

El momento de compra entra por los "meses de renta pendientes" (desde hoy hasta
el fin): quien compra un proyecto ya en marcha solo cobra las rentas que quedan.

Todas las cifras van en unidades de porcentaje (12.0 = 12 %), igual que maestro.py.
El precio de compra debe venir en la MISMA divisa que el precio de emisión.
"""
from __future__ import annotations

SUFIJO = {"Reentel": "rnt", "ReentelPro": "rp", "SuperReentel": "sr"}


def _num(valor):
    try:
        if valor is None or str(valor).strip() == "":
            return None
        v = float(str(valor).replace("%", "").replace(",", ".").strip())
        return None if v != v else v          # NaN -> None
    except (TypeError, ValueError):
        return None


def _campo(row, nombre, sufijo):
    """El valor de ese estatus o, si no hay, el de Reentel (proyectos sin estatus)."""
    v = _num(row.get(f"{nombre}_{sufijo}"))
    return v if v is not None else _num(row.get(f"{nombre}_rnt"))


def rentabilidad_ajustada(row, estatus: str, precio_compra=None) -> dict:
    """{"renta", "plusvalia", "total", "anual", "meses"} para ese estatus y precio.

    `row` es una fila del DataFrame de proyectos (o un dict) con las claves de
    maestro.py: est_recurr_*, est_plusvalia_*, real_recurr_*, real_plusvalia_*,
    y además 'Precio Emisión', 'Meses Pendientes' y 'ESTADO'.
    """
    suf = SUFIJO.get(estatus or "Reentel", "rnt")
    estado = str(row.get("ESTADO", "") or "").upper()

    renta = _campo(row, "real_recurr", suf)
    if not renta:                                   # sin renta real (o 0): la estimada
        renta = _campo(row, "est_recurr", suf)
    renta = renta or 0.0

    plusvalia = _campo(row, "real_plusvalia", suf) if "CERRAD" in estado else None
    if plusvalia is None:
        plusvalia = _campo(row, "est_plusvalia", suf)
    plusvalia = plusvalia or 0.0

    emision = _num(row.get("Precio Emisión")) or 100.0
    compra = _num(precio_compra) or emision
    meses = _num(row.get("Meses Pendientes")) or 0.0
    # Para anualizar se usan los meses hasta el CIERRE si el Master los tiene y
    # son más que los de renta: el dinero sigue invertido hasta que el proyecto
    # termina, aunque la renta se acabe antes (p. ej. proyectos en prórroga).
    meses_fin = _num(row.get("meses_hasta_fin")) or 0.0
    meses_anualizar = max(meses, meses_fin)

    renta_aj = renta * emision / compra
    plusv_aj = (emision * (1 + plusvalia / 100) - compra) / compra * 100
    total = renta_aj / 12 * meses + plusv_aj
    anual = total * 12 / meses_anualizar if meses_anualizar > 0 else None
    return {"renta": renta_aj, "plusvalia": plusv_aj, "total": total,
            "anual": anual, "meses": meses_anualizar}


# ─── Ganancia a un plazo, cobrando cada cosa cuando se paga ─────────────────
# Rentabilidad anual a la que se reinvierte lo cobrado (rentas, y capital +
# beneficio cuando un proyecto termina), según el estatus del cliente.
TASA_REINVERSION = {"Reentel": 11.0, "ReentelPro": 13.0, "SuperReentel": 16.0}


def frecuencia_pagos(tipologia) -> int:
    """Cada cuántos meses paga rentas el proyecto (0 = solo al final, tipo bullet)."""
    t = str(tipologia or "").lower()
    if "mensual" in t:
        return 1
    if "trimestral" in t:
        return 3
    if "semestral" in t:
        return 6
    if "anual" in t and "final" not in t:
        return 12
    return 0


def ganancia_proyecto(importe, row, estatus: str, precio_compra, meses_plazo) -> float:
    """Ganancia de invertir `importe` en el proyecto al cabo de `meses_plazo` meses.

    - Las rentas se cobran cuando se pagan (cada mes, cada trimestre…) y desde ese
      momento se reinvierten a TASA_REINVERSION del estatus.
    - La plusvalía (y todo el beneficio de los proyectos que pagan al final) se va
      ganando poco a poco, pero no se reinvierte hasta que el proyecto termina.
    - Al terminar, el capital y la plusvalía se reinvierten a TASA_REINVERSION.
    """
    if not importe or importe <= 0:
        return 0.0
    r = rentabilidad_ajustada(row, estatus, precio_compra)
    fin = max(int(round(r["meses"] or 0)), 0)               # meses hasta que termina
    meses_renta = min(int(round(_num(row.get("Meses Pendientes")) or 0)), fin)
    inicio_renta = fin - meses_renta                         # la renta se cobra al final del plazo del proyecto
    f = frecuencia_pagos(row.get("Tipología de Dividendo"))
    renta = r["renta"] if f else 0.0
    plusvalia = r["plusvalia"] if f else r["total"]          # bullet: todo se cobra al final
    tasa = TASA_REINVERSION.get(estatus or "Reentel", 11.0) / 100 / 12
    H = int(meses_plazo)

    def crece(cantidad, desde_mes):                          # lo cobrado en `desde_mes`, reinvertido hasta H
        return cantidad * (1 + tasa) ** max(H - desde_mes, 0)

    ganancia = 0.0
    if f and renta:
        cobro = importe * renta / 100 * f / 12
        for m in range(inicio_renta + f, min(fin, H) + 1, f):
            ganancia += crece(cobro, m)
    if fin <= H:
        ganancia += crece(importe * plusvalia / 100, fin)    # plusvalía cobrada al terminar
        ganancia += crece(importe, fin) - importe            # el capital vuelve y se reinvierte
    elif fin > 0:
        ganancia += importe * plusvalia / 100 * H / fin      # aún no cobrada: se va ganando, sin reinvertir
    return ganancia
