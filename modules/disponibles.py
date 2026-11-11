"""
PROYECTOS QUE SE PUEDEN COMPRAR HOY - la misma lista que el Paso 3 del simulador.

- Los que se están financiando, a su precio de emisión.
- Los que tienen tokens en OTC (stock de Reental u ofertas de terceros, contrastadas
  con la cadena y descontando reservas), con al menos MIN_TOKENS_OTC tokens, a su
  precio más bajo.
- Sin los que terminan en menos de MESES_MINIMOS_HASTA_FIN meses.

Es una copia de la lógica de modules/comun.py del simulador (catalogo_otc,
agrupar_ofertas_por_proyecto y proyectos_comprables). Si se cambia allí, hay que
cambiarlo también aquí para que las dos herramientas ofrezcan lo mismo.
"""
from __future__ import annotations

from datetime import date

import streamlit as st

OTC_WALLET_CUSTODIA = "0xce0719ec1bda336ba069c6961ad167767829301a"
MIN_TOKENS_OTC = 10
MESES_MINIMOS_HASTA_FIN = 4


def _num(valor, defecto=0.0):
    try:
        x = float(str(valor).replace(",", "."))
        return defecto if x != x else x
    except (TypeError, ValueError):
        return defecto


def proyectos_para_catalogo(df):
    """Tupla (hashable, para la caché) con lo que el catálogo OTC necesita de cada proyecto."""
    filas = []
    for _, r in df.iterrows():
        addr = str(r.get("Token Address", "") or "").strip().lower()
        if not (addr.startswith("0x") and len(addr) == 42):
            continue
        filas.append((addr, str(r.get("ID", "") or "").strip(), str(r.get("Nombre del proyecto", "") or ""),
                      str(r.get("Divisa", "") or "EUR"), _num(r.get("Precio Emisión", 0)),
                      str(r.get("Ubicación", "") or ""), str(r.get("ESTADO", "") or "")))
    return tuple(filas)


@st.cache_data(ttl=86400, show_spinner=False)    # un día
def catalogo_otc(proyectos, api_key):
    """Lo disponible hoy en OTC (mismos módulos que la herramienta OTC). Se guarda un día."""
    import otc_inventario
    from modules import otc_storage

    try:
        custodia = str(st.secrets["otc"]["custodia"]).strip().lower()
    except Exception:
        custodia = OTC_WALLET_CUSTODIA
    por_addr, por_id = {}, {}
    for addr, pid, nombre, divisa, precio, ubic, estado in proyectos:
        p = {"id": pid, "nombre": nombre, "token_address": addr, "divisa": divisa, "precio_emision": precio,
             "ubicacion": ubic, "estado": estado, "fecha_fin": None, "tipo_renta": "—"}
        por_addr[addr] = p
        if pid:
            por_id[pid.lower()] = p
    try:
        otc_storage._libro().worksheet("Ofertas")
    except Exception as e:
        print(f"❌ No se puede leer el Sheet OTC: {e}. Compártelo (Lector) con el client_email de "
              "[gcp_service_account] de los Secrets.")
        return {"catalogo": {}, "precios": {}, "error": "sin_acceso_sheet"}
    reservas = otc_storage.read_list("Reservas")
    ofertas = otc_storage.read_list("Ofertas")
    balances, entradas = {}, {}
    if custodia:
        balances, _envios, entradas, _ = otc_inventario.balances_de_wallet(custodia, api_key, por_addr, por_id)
    cat = otc_inventario.catalogo(balances, reservas, ofertas, api_key, otc_inventario.saldo_en_wallet,
                                  entradas, custodia)
    try:
        precios = otc_storage.read_dict("precios_otc")
    except Exception:
        precios = {}
    return {"catalogo": cat, "precios": precios, "error": ""}


def _precio_stock_reental(precios, addr):
    v = (precios.get(addr) or {}).get("precio_otc") if isinstance(precios.get(addr), dict) else None
    valor = _num(v, None)
    return valor if valor and valor > 0 else None


def termina_pronto(fecha_fin, meses=MESES_MINIMOS_HASTA_FIN):
    if not hasattr(fecha_fin, "year"):
        return False
    return (fecha_fin - date.today()).days < meses * 30.4375


def _a_divisa(importe, desde, hacia, usd_por_eur):
    desde = "USD" if str(desde).upper() in ("USD", "USDT", "$") else "EUR"
    hacia = "USD" if str(hacia).upper() in ("USD", "USDT", "$") else "EUR"
    if desde == hacia:
        return importe
    return importe * usd_por_eur if desde == "EUR" else importe / usd_por_eur


def comprables(df, api_key, usd_por_eur):
    """({token_address: {"precio" (en la divisa del proyecto), "origen": "Financiación" | "Mercado"}}, error).
    error es "" si se ha podido leer el OTC; si no, solo salen los que se están financiando."""
    try:
        res = catalogo_otc(proyectos_para_catalogo(df), api_key)
    except Exception as e:
        print(f"❌ Error calculando la disponibilidad OTC: {e}")
        res = {"catalogo": {}, "precios": {}, "error": "fallo"}
    cat, precios_otc = res.get("catalogo") or {}, res.get("precios") or {}

    resultado = {}
    for _, row in df.iterrows():
        addr = str(row.get("Token Address", "") or "").strip().lower()
        if not addr.startswith("0x") or "CERRAD" in str(row.get("ESTADO", "")).upper():
            continue
        if termina_pronto(row.get("fecha_fin_calculo")):
            continue
        div = "USD" if str(row.get("Divisa", "") or "").strip().upper() in ("USD", "$", "USDT") else "EUR"
        if str(row.get("ESTADO", "")).upper() == "FINANCIÁNDOSE":
            precio, origen = _num(row.get("Precio Emisión")), "Financiación"
        else:
            d = cat.get(addr) or {}
            precios = []
            if (d.get("reental") or 0) >= MIN_TOKENS_OTC:
                p = _precio_stock_reental(precios_otc, addr) or _num(row.get("Precio Emisión"))
                precios.append(p)                                    # en la divisa del proyecto
            for t in d.get("terceros") or []:
                if t.get("disponible", 0) >= MIN_TOKENS_OTC and _num(t.get("precio")) > 0:
                    precios.append(_a_divisa(_num(t["precio"]), t.get("divisa", "USD"), div, usd_por_eur))
            if not precios:
                continue
            precio, origen = min(precios), "Mercado"
        if precio > 0:
            resultado[addr] = {"precio": precio, "origen": origen}
    return resultado, res.get("error", "")
