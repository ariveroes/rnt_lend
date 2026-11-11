"""
RNT LEND - ¿Cuánto puedo pedir?

Herramienta pública: el usuario pone su wallet (o elige a mano sus inmuebles y
tokens) y el proyecto en el que quiere invertir. Calcula cuánto puede pedir hoy en
RNT Lend con sus inmuebles como garantía para no pagar nada hasta que ese proyecto
termine, sin que la deuda llegue a superar el 80 % de la garantía (donde se liquida).

ESTRUCTURA
  app.py                      → esta pantalla
  modules/calculo_lend.py     → el cálculo (supuestos explicados al principio)
  modules/aave_lend.py        → lectura de RNT Lend en la cadena (posición, LTV,
                                umbral de liquidación, precios del oráculo, tipos)
  modules/maestro.py, data_loader.py, rentabilidad_compra.py → proyectos del Master
  modules/utils.py, reental_tokens.py → movimientos de la wallet
  modules/divisas.py          → tipo de cambio €/$
"""
import os
import sys
from datetime import date

import streamlit as st

st.set_page_config(page_title="¿Cuánto puedo pedir en RNT Lend?", page_icon="🏦", layout="wide")

_DIR_MODULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modules")
if _DIR_MODULES not in sys.path:          # los módulos de la cadena se importan entre sí sin «modules.»
    sys.path.insert(0, _DIR_MODULES)

import altair as alt
import pandas as pd

from modules import aave_lend, calculo_lend, data_loader, disponibles
from modules.rentabilidad_compra import TASA_REINVERSION, ganancia_proyecto, rentabilidad_ajustada

URL_AGENDA = "https://api.leadconnectorhq.com/widget/booking/kAzM5NH9hFxtc88sTQKy"   # la del simulador
USDT_POLYGON = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
APR_DEFECTO = 0.10
USD_POR_EUR_DEFECTO = 1.14
SIMBOLOS_RNT_EN_ESTATUS = ("xrnt",)    # RNT puestos para conseguir el estatus
RNT_POR_ESTATUS = {"SuperReentel": 28000, "ReentelPro": 14000}
ESTATUS = ["Reentel", "ReentelPro", "SuperReentel"]
MESES_MINIMOS = 4                     # como en el simulador: no se proponen proyectos que terminan ya
NAVY, AMBAR, AMBAR_OSCURO, GRIS, ROJO = "#1f2937", "#fca311", "#d97706", "#6b7280", "#dc2626"
MESES_ES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
            "octubre", "noviembre", "diciembre")


def es(v, dec=0):
    return f"{v:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def mes_texto(fecha):
    return f"{MESES_ES[fecha.month - 1]} de {fecha.year}"


def meses_hasta(fecha):
    if not hasattr(fecha, "year"):
        return None
    return (fecha - date.today()).days / 30.4375


def _num(v, defecto=0.0):
    try:
        x = float(str(v).replace(",", ".").replace("%", "").strip())
        return defecto if x != x else x
    except (TypeError, ValueError):
        return defecto


def dos(usd, signo=False):
    """«5.546 $ · 4.781 €»: los importes se muestran en las dos divisas (el préstamo es en USDT)."""
    s = ("+" if usd >= 0 else "−") if signo else ""
    return (f"{s}{es(abs(usd))} $<span style='font-size:0.62em; color:{GRIS}; font-weight:700;'> · "
            f"{s}{es(abs(usd) / USD_EUR[0])} €</span>")


USD_EUR = [USD_POR_EUR_DEFECTO]      # se rellena con el tipo del día al cargar


def tarjeta(titulo, valor, sub="", destacada=False, alto=False, color=None):
    fondo, borde, color_def = ("#fff4de", AMBAR, AMBAR_OSCURO) if destacada else ("#ffffff", "#e5e7eb", NAVY)
    color = color or color_def
    # alto=True: las tres tarjetas de la situación actual, todas del mismo tamaño
    return (f"<div style='background:{fondo}; border:2px solid {borde}; border-radius:14px; padding:14px 18px; "
            f"height:100%; {'min-height:140px; box-sizing:border-box;' if alto else ''}'><div style='color:{GRIS}; font-size:13px; font-weight:600;'>{titulo}</div>"
            f"<div style='font-size:28px; font-weight:800; color:{color}; line-height:1.25; margin:4px 0;'>{valor}</div>"
            f"<div style='color:{NAVY}; font-size:13px;'>{sub}</div></div>")


def api_key():
    try:
        return st.secrets["etherscan"]["api_key"]
    except Exception:
        st.error("Falta la clave de Etherscan en los Secrets ([etherscan] api_key).")
        st.stop()


# ---------- Datos (con caché) ----------
@st.cache_data(ttl=3600, show_spinner=False)
def _usd_por_eur():
    from modules import divisas
    return divisas.usd_por_euro() or USD_POR_EUR_DEFECTO


@st.cache_data(ttl=600, show_spinner=False)
def _pool(clave):
    """Activos del pool (LTV, umbral, aToken) y sus precios en el oráculo."""
    activos = aave_lend.activos_garantia(clave)
    precios = aave_lend.get_asset_prices(tuple(sorted(activos)), clave) if activos else {}
    return activos, precios


@st.cache_data(ttl=21600, show_spinner=False)
def _tipo_interes(clave):
    """(APR, de dónde sale). Media histórica del préstamo en USDT; si no, el actual."""
    medio = aave_lend.apr_borrow_medio_historico(USDT_POLYGON, clave)
    if medio:
        return medio, "media histórica del préstamo en USDT"
    actual = (aave_lend.config_de_reserva(USDT_POLYGON, clave) or {}).get("borrow_rate_apr")
    if actual:
        return actual, "tipo actual del préstamo en USDT"
    return APR_DEFECTO, "estimado"


@st.cache_data(ttl=300, show_spinner=False)
def _leer_wallet(wallet, clave, activos_lista):
    """Posición real en RNT Lend y tokens de inmuebles de la wallet.
    Devuelve (cuenta, {token_address: {"garantia": tokens en RNT Lend, "wallet": tokens sueltos}})."""
    from utils import fetch_all_token_txs
    activos = dict(activos_lista)
    cuenta = aave_lend.get_user_account_data(wallet, clave)
    txs = fetch_all_token_txs(wallet, clave)
    tocados = {tx["contractAddress"].lower() for tx in txs}
    # RNT puestos para el estatus (token xRNT): entradas menos salidas
    rnt_estatus = 0.0
    for tx in txs:
        if str(tx.get("tokenSymbol", "")).lower() in SIMBOLOS_RNT_EN_ESTATUS:
            valor = int(tx.get("value", 0)) / 10 ** int(tx.get("tokenDecimal") or 18)
            if str(tx.get("to", "")).lower() == wallet:
                rnt_estatus += valor
            elif str(tx.get("from", "")).lower() == wallet:
                rnt_estatus -= valor
    tokens = {}
    for addr, cfg in activos.items():
        if addr not in tocados and cfg.get("atoken", "").lower() not in tocados:
            continue
        dec = cfg.get("decimales") or 18
        en_garantia = aave_lend.balance_of(cfg["atoken"], wallet, dec, clave) if cfg.get("atoken") else 0.0
        suelto = aave_lend.balance_of(addr, wallet, dec, clave)
        if en_garantia > 0.0001 or suelto > 0.0001:
            tokens[addr] = {"garantia": en_garantia, "wallet": suelto}
    return cuenta, tokens, max(0.0, rnt_estatus)


# ---------- Cabecera ----------
st.markdown(f"<div style='font-size:34px; font-weight:800; color:{NAVY}; line-height:1.2;'>"
            f"Posición de mi cartera en RNT Lend</div>", unsafe_allow_html=True)
st.caption("Revisa tu situación actual en RNT Lend y simula cuánta financiación extra podrías pedir usando tus "
           "tokens como garantía, sin pagar nada hasta que termine el proyecto en el que inviertas. También puedes "
           "crear tu propia cartera inmobiliaria para sacarle el mayor beneficio al RNT Lend.")

clave = api_key()
with st.spinner("Leyendo los proyectos y RNT Lend…"):
    df = data_loader.cargar_todos_proyectos()
    activos, precios = _pool(clave)
    usd_por_eur = _usd_por_eur()
    USD_EUR[0] = usd_por_eur
if df is None or df.empty or not activos:
    st.error("Ahora mismo no podemos leer los proyectos o RNT Lend. Prueba de nuevo en unos minutos.")
    st.stop()

por_addr = {str(r.get("Token Address", "") or "").strip().lower(): r for _, r in df.iterrows()
            if str(r.get("Token Address", "") or "").startswith("0x")}
# Lo que se puede comprar hoy y a qué precio: la misma lista que el Paso 3 del simulador
with st.spinner("Revisando los proyectos disponibles…"):
    comprables, error_otc = disponibles.comprables(df, clave, usd_por_eur)


# ---------- Diagnóstico oculto (?diagnostico=1): por qué sale o no cada proyecto ----------
if str(st.query_params.get("diagnostico", "") or "") == "1":
    def _f(v):
        return v.strftime("%d/%m/%Y") if hasattr(v, "strftime") else "—"

    st.markdown("## Diagnóstico de proyectos")
    st.caption("Para salir, quita «?diagnostico=1» de la dirección.")
    if error_otc:
        st.error("No se ha podido leer el documento OTC: solo cuentan los proyectos que se están financiando.")
    filas_d = []
    for _, r in df.iterrows():
        addr = str(r.get("Token Address", "") or "").strip().lower()
        fin = r.get("fecha_fin_calculo")
        m = meses_hasta(fin)
        if not addr.startswith("0x"):
            motivo = "Sin Token Address en el Master"
        elif "CERRAD" in str(r.get("ESTADO", "")).upper():
            motivo = "Cerrado"
        elif fin is None or not hasattr(fin, "year"):
            motivo = "Sin fecha de fin (ni auxiliar, ni real, ni I, ni F)"
        elif m is not None and m < MESES_MINIMOS:
            motivo = f"Termina en menos de {MESES_MINIMOS} meses"
        elif addr not in comprables:
            motivo = ("No está en financiación ni tiene 10 tokens o más en OTC"
                      if str(r.get("ESTADO", "")).upper() != "FINANCIÁNDOSE" else "Sin precio de emisión")
        else:
            motivo = ""
        filas_d.append({
            "Proyecto": r.get("Nombre del proyecto", ""), "Estado": r.get("ESTADO", ""),
            "Token Address": "Sí" if addr.startswith("0x") else "No",
            "Financiación (G)": _f(r.get("fecha_financiacion")), "Fin desde financiación (I)": _f(r.get("fecha_fin_estimada")),
            "Fin desde lanzamiento (F)": _f(r.get("fecha_fin_lanzamiento")), "Fin real": _f(r.get("fecha_fin_real")),
            "Fin auxiliar (CA)": _f(r.get("fecha_fin_auxiliar")), "Fecha de fin usada": _f(fin),
            "Precio emisión": r.get("Precio Emisión", ""),
            "¿Sale en la lista?": "Sí" if not motivo else "No", "Motivo": motivo})
    st.dataframe(pd.DataFrame(filas_d).sort_values(["¿Sale en la lista?", "Proyecto"]), hide_index=True,
                 use_container_width=True)
    st.stop()


def nombre_de(addr):
    r = por_addr.get(addr)
    return str(r.get("Nombre del proyecto")) if r is not None else addr[:10] + "…"


def dossier_de(addr):
    """Enlace al dossier comercial del proyecto (columna «Link a Dossier Comercial» del Master) o None."""
    r = por_addr.get(addr)
    url = str(r.get("dossier", "") or "").strip() if r is not None else ""
    return url if url.lower().startswith("http") else None


def sirve_de_garantia(addr):
    cfg = activos.get(addr) or {}
    return cfg.get("ltv", 0) > 0 and cfg.get("activa", True) and not cfg.get("congelada", False)


def garantia_de(addr, tokens):
    """Una línea de garantía para el cálculo."""
    cfg = activos.get(addr) or {}
    r = por_addr.get(addr)
    fin = r.get("fecha_fin_calculo") if r is not None else None
    return {"nombre": nombre_de(addr), "valor_usd": tokens * precios.get(addr, 0.0),
            "ltv": cfg.get("ltv", 0.0), "umbral": cfg.get("umbral", 0.0),
            "meses_fin": meses_hasta(fin), "fecha_fin": fin}


def color_hf(hf):
    if hf is None:
        return "#15803d"
    return ROJO if hf < 1.1 else (AMBAR_OSCURO if hf < 1.5 else "#15803d")


# ---------- Bloque 1: tu situación actual ----------
modo = st.radio("¿Cómo quieres ver tu cartera?",
                ["Con mi wallet", "Aún no invierto: simular una cartera"], horizontal=True)

garantias, deuda0, tokens, rnt_estatus = [], 0.0, {}, 0.0
if modo == "Con mi wallet":
    st.markdown("## Tu situación actual")
    texto_wallets = st.text_area(
        "Dirección de tu wallet", placeholder="0x…\n0x… (si tienes varias, una por línea)", height=88,
        help="Puedes poner varias wallets, una por línea: verás tu posición total y la de cada una.")
    if st.button("Ver mi posición", type="primary"):
        wallets = []
        for w in texto_wallets.replace(",", "\n").replace(";", "\n").split():
            w = w.strip().lower()
            if w and w not in wallets:
                wallets.append(w)
        malas = [w for w in wallets if not (w.startswith("0x") and len(w) == 42)]
        if not wallets or malas:
            st.error("Revisa las direcciones: cada una empieza por 0x y tiene 42 caracteres."
                     + (f" No parece válida: {malas[0]}" if malas else ""))
        else:
            with st.spinner("Leyendo tu posición en RNT Lend… (puede tardar unos segundos por wallet)"):
                st.session_state.lend_wallets = [
                    (w,) + _leer_wallet(w, clave, tuple(sorted(activos.items()))) for w in wallets]
    leidas = st.session_state.get("lend_wallets")
    if not leidas:
        st.stop()
    if any(not c for _, c, _, _ in leidas):
        st.warning("No hemos podido leer alguna de tus wallets en RNT Lend. Prueba de nuevo en unos minutos.")

    # Posición total: se suman garantía, deuda y tokens de todas las wallets
    tokens, rnt_estatus = {}, 0.0
    colateral = deuda0 = garantia_umbral = 0.0
    for _, c, toks, rnt_w in leidas:
        c = c or {}
        colateral += c.get("colateral_usd", 0.0)
        deuda0 += c.get("deuda_usd", 0.0)
        garantia_umbral += c.get("colateral_usd", 0.0) * (c.get("umbral_liquidacion") or 0.8)
        rnt_estatus = max(rnt_estatus, rnt_w)
        for a, t in toks.items():
            acum = tokens.setdefault(a, {"garantia": 0.0, "wallet": 0.0})
            acum["garantia"] += t["garantia"]
            acum["wallet"] += t["wallet"]
    hf = (garantia_umbral / deuda0) if deuda0 > 0.01 else None
    varias = len(leidas) > 1
    k1, k2, k3 = st.columns(3)
    k1.markdown(tarjeta("¿Cuánto tengo ya aportado como garantía en RNT Lend?", dos(colateral),
                        "Valor de tus tokens en garantía según RNT Lend" + (" (todas tus wallets)" if varias else ""),
                        alto=True), unsafe_allow_html=True)
    k2.markdown(tarjeta("¿A cuánto asciende mi deuda actualmente?", dos(deuda0),
                        "Lo que pediste más los intereses acumulados", alto=True), unsafe_allow_html=True)
    k3.markdown(tarjeta("¿Cuál es mi factor de salud?" + (" (conjunto)" if varias else ""),
                        es(hf, 2) if hf else "Sin deuda",
                        "Se liquida si baja de 1, es decir, cuando tu deuda llega al 80&nbsp;% de tu garantía."
                        if hf else "Sin riesgo de liquidación.", alto=True, color=color_hf(hf)),
                unsafe_allow_html=True)
    if varias:
        st.write("")
        st.dataframe(pd.DataFrame([{
            "Wallet": f"{w[:6]}…{w[-4:]}",
            "Garantía": f"{es((c or {}).get('colateral_usd', 0))} $",
            "Deuda": f"{es((c or {}).get('deuda_usd', 0))} $",
            "Factor de salud": (es(c["health_factor"], 2) if c and c.get("health_factor") else "Sin deuda"),
        } for w, c, _, _ in leidas]), hide_index=True, use_container_width=True)
        st.caption("En RNT Lend cada wallet es una posición independiente y se liquida por separado. Arriba ves "
                   "el total y la simulación de abajo suma todos tus tokens como si estuvieran en una sola wallet: "
                   "para pedir con toda esa garantía, tendrían que estar juntos en la misma.")
    st.write("")
    filas = []
    for addr, t in tokens.items():
        r = por_addr.get(addr)
        fin = r.get("fecha_fin_calculo") if r is not None else None
        m = meses_hasta(fin)
        nombre = nombre_de(addr)
        vencido = m is not None and m <= 0
        if not sirve_de_garantia(addr):
            nombre += " · no admite garantía"
        elif vencido:
            nombre += " · ya pasó su fecha de fin, no cuenta"
        total = t["garantia"] + t["wallet"]
        filas.append({
            "Proyecto": nombre,
            "% de tus tokens en garantía": f"{es(t['garantia'] / total * 100)} %" if total > 0 else "—",
            "Tokens adicionales que puedes añadir como garantía": (es(t["wallet"], 2)
                                                                   if sirve_de_garantia(addr) and not vencido
                                                                   else "—"),
            "Fin estimado": fin.strftime("%m/%Y") if hasattr(fin, "year") else "—"})
    if filas:
        st.dataframe(pd.DataFrame(filas), hide_index=True, use_container_width=True)
    else:
        st.info("No hemos encontrado tokens de inmuebles en esta wallet.")
else:
    st.markdown("## Tu cartera simulada")
    c_div, c_imp_cart = st.columns([1, 2])
    divisa_cli = c_div.radio("**¿En qué divisa quieres invertir?**", ["EUR", "USD"], horizontal=True,
                             format_func=lambda d: "Euros (€)" if d == "EUR" else "Dólares ($)")
    simb = "€" if divisa_cli == "EUR" else "$"
    importe_cart = c_imp_cart.number_input(f"**¿Cuánto quieres invertir? ({simb})**", min_value=0.0, value=20000.0,
                                           step=1000.0, format="%.0f")
    st.markdown("**¿Qué estatus tienes o te gustaría tener?**")
    estatus_cartera = st.selectbox("Estatus", ESTATUS, label_visibility="collapsed", key="estatus_cartera")
    st.markdown(f"<div style='background:#fff4de; border-left:4px solid {AMBAR}; border-radius:8px; "
                f"padding:10px 14px; color:{NAVY}; font-size:14px; margin:4px 0 12px;'>¿Quieres entender mejor "
                f"cómo te beneficia cada estatus? <a href='{URL_AGENDA}' target='_blank' rel='noopener' "
                f"style='color:{AMBAR_OSCURO}; font-weight:700;'>Agenda una llamada con nuestro equipo</a>.</div>",
                unsafe_allow_html=True)

    opciones = sorted([a for a in comprables if sirve_de_garantia(a) and a in por_addr], key=nombre_de)
    if not opciones:
        st.info("Ahora mismo no hay proyectos disponibles que se puedan usar como garantía.")
        st.stop()

    def _div(a):
        return "USD" if str(por_addr[a].get("Divisa", "")).upper() in ("USD", "$", "USDT") else "EUR"

    def _a_cliente(importe, desde):
        """De la divisa de un proyecto a la del cliente."""
        if desde == divisa_cli:
            return importe
        return importe * usd_por_eur if desde == "EUR" else importe / usd_por_eur

    # Tabla para elegir proyectos, con la misma información que el simulador
    filas_t = []
    for a in opciones:
        r = por_addr[a]
        pr = comprables[a]["precio"]
        ra = rentabilidad_ajustada(r, estatus_cartera, pr)       # con el precio y el momento de compra
        txt_precio = f"{es(pr, 2)} {'$' if _div(a) == 'USD' else '€'}"
        if _div(a) != divisa_cli:
            txt_precio += f" (≈ {es(_a_cliente(pr, _div(a)), 2)} {simb})"
        filas_t.append({"Elegir": False, "Proyecto": nombre_de(a), "Ubicación": str(r.get("Ubicación", "") or ""),
                        "Precio por token": txt_precio,
                        "Tipo de rendimiento": str(r.get("Tipología de Dividendo", "") or "—"),
                        f"Rent. anualizada ({estatus_cartera})": f"{es(ra['anual'] or 0.0, 2)} %",
                        f"Rent. total ({estatus_cartera})": f"{es(ra['total'] or 0.0, 2)} %",
                        "Dossier": dossier_de(a)})
    st.markdown("**Elige los proyectos de tu cartera**")
    st.caption("Proyectos disponibles hoy que se pueden usar como garantía en RNT Lend. Rentabilidades estimadas con "
               "tu estatus, según el precio y el momento de compra.")
    elegidos_df = st.data_editor(
        pd.DataFrame(filas_t), hide_index=True, use_container_width=True, key=f"elegir_{estatus_cartera}_{divisa_cli}",
        disabled=[c for c in filas_t[0] if c != "Elegir"],
        column_config={"Elegir": st.column_config.CheckboxColumn(width="small"),
                       "Dossier": st.column_config.LinkColumn(display_text="Dossier", width="small")})
    elegidos = [a for a, m in zip(opciones, elegidos_df["Elegir"]) if bool(m)]

    if elegidos and importe_cart > 0:
        st.markdown("**¿Cómo quieres repartir tu inversión?**")
        reparto = st.radio("Reparto", ["Por igual", "Por % de mi cartera"], horizontal=True,
                           label_visibility="collapsed")
        if reparto == "Por igual":
            pesos = {a: 100.0 / len(elegidos) for a in elegidos}
        else:
            base_pct = pd.DataFrame({"Proyecto": [nombre_de(a) for a in elegidos],
                                     "% de tu cartera": [round(100.0 / len(elegidos), 2)] * len(elegidos)})
            pct_df = st.data_editor(base_pct, hide_index=True, use_container_width=True,
                                    key=f"pct_{'_'.join(sorted(elegidos))}", disabled=["Proyecto"],
                                    column_config={"% de tu cartera": st.column_config.NumberColumn(
                                        min_value=0.0, max_value=100.0, step=1.0, format="%.2f %%")})
            pesos = {a: _num(v) for a, v in zip(elegidos, pct_df["% de tu cartera"])}
            suma = sum(pesos.values())
            if abs(suma - 100) > 0.01:
                st.warning(f"Los porcentajes suman {es(suma, 2)} %: tienen que sumar 100 %.")
                st.stop()
        resumen = []
        for a in elegidos:
            imp_cli = importe_cart * pesos[a] / 100                       # en la divisa del cliente
            imp_proy = imp_cli if _div(a) == divisa_cli else (
                imp_cli / usd_por_eur if divisa_cli == "USD" else imp_cli * usd_por_eur)
            n_tok = imp_proy / comprables[a]["precio"]
            garantias.append(garantia_de(a, n_tok))
            resumen.append(f"{nombre_de(a)}: {es(imp_cli)} {simb} ({es(n_tok, 1)} tokens)")
        st.caption("Tu cartera: " + " · ".join(resumen))
    estatus_fijado = estatus_cartera

# ---------- Bloque 2: simula una financiación extra ----------
st.markdown("## Simula una financiación extra")
if modo == "Con mi wallet":
    def _cuenta(addr):
        m = meses_hasta(por_addr[addr].get("fecha_fin_calculo")) if addr in por_addr else None
        return sirve_de_garantia(addr) and not (m is not None and m <= 0)

    sueltos = [a for a, t in tokens.items() if t["wallet"] > 0.0001 and _cuenta(a)]
    anadir = {}
    if sueltos:
        st.markdown("**¿Quieres considerar los tokens adicionales que tienes en la wallet para aumentar tu "
                    "garantía?**")
        opcion = st.radio("Tokens adicionales", ["Sí (indica a continuación cuánto quieres aportar como garantía adicional)", "No"],
                          label_visibility="collapsed")
        if opcion.startswith("Sí"):
            st.caption("Marca «Añadir el 100 %» para aportar todos los tokens disponibles, o desmárcalo y escribe "
                       "cuántos quieres aportar.")
            col_escribe = (f"O escribe cuántos tokens (máximo {es(tokens[sueltos[0]]['wallet'], 2)})"
                           if len(sueltos) == 1 else "O escribe cuántos tokens (máximo: los disponibles)")
            # Valores de partida de la tabla. Se guardan para poder rehacerla (con otra clave) cuando alguien
            # escribe una cantidad con «Añadir el 100 %» marcado: así esa cantidad desaparece al momento.
            if st.session_state.get("anadir_para") != tuple(sueltos):
                st.session_state.anadir_para = tuple(sueltos)
                st.session_state.anadir_vals = {a: (True, 0.0) for a in sueltos}
                st.session_state.anadir_ver = st.session_state.get("anadir_ver", 0) + 1
            vals = st.session_state.anadir_vals
            base = pd.DataFrame({"Proyecto": [nombre_de(a) for a in sueltos],
                                 "Disponibles en tu wallet": [round(tokens[a]["wallet"], 2) for a in sueltos],
                                 "Añadir el 100 %": [vals[a][0] for a in sueltos],
                                 col_escribe: [vals[a][1] for a in sueltos]})
            editado = st.data_editor(
                base, hide_index=True, use_container_width=True, key=f"tokens_anadir_{st.session_state.anadir_ver}",
                disabled=["Proyecto", "Disponibles en tu wallet"],
                column_config={
                    "Disponibles en tu wallet": st.column_config.NumberColumn(format="%.2f"),
                    "Añadir el 100 %": st.column_config.CheckboxColumn(),
                    col_escribe: st.column_config.NumberColumn(
                        min_value=0.0, step=1.0, format="%.2f",
                        help="Solo si desmarcas «Añadir el 100 %»")})
            nuevos = {a: (bool(todo), 0.0 if bool(todo) else _num(n))
                      for a, todo, n in zip(sueltos, editado["Añadir el 100 %"], editado[col_escribe])}
            if any(bool(t) and _num(n) > 0 for t, n in zip(editado["Añadir el 100 %"], editado[col_escribe])):
                st.session_state.anadir_vals = nuevos
                st.session_state.anadir_ver += 1
                st.rerun()
            excedidos = []
            for a, todo, n in zip(sueltos, editado["Añadir el 100 %"], editado[col_escribe]):
                disp = tokens[a]["wallet"]
                if bool(todo):
                    anadir[a] = disp
                else:
                    if _num(n) > disp + 1e-9:
                        excedidos.append(a)
                    anadir[a] = min(_num(n), disp)
            for a in excedidos:
                st.warning(f"En {nombre_de(a)} puedes añadir como máximo {es(tokens[a]['wallet'], 2)} tokens como "
                           f"garantía, que son los que tienes disponibles.")
    for addr, t in tokens.items():
        n = t["garantia"] + anadir.get(addr, 0.0)
        if sirve_de_garantia(addr) and n > 0:
            garantias.append(garantia_de(addr, n))

detectado = ("SuperReentel" if rnt_estatus >= RNT_POR_ESTATUS["SuperReentel"]
             else "ReentelPro" if rnt_estatus >= RNT_POR_ESTATUS["ReentelPro"] else "Reentel")
if modo == "Con mi wallet":
    st.markdown("**Tu estatus**")
    estatus = st.selectbox(
        "Tu estatus", ESTATUS, index=ESTATUS.index(detectado), label_visibility="collapsed",
        help="Lo hemos detectado por los RNT que tienes en estatus en tu wallet; cámbialo si no es correcto.")
else:
    estatus = estatus_fijado
st.markdown("**¿En qué proyecto quieres invertir?**")
candidatos = [a for a in comprables if a in por_addr and meses_hasta(por_addr[a].get("fecha_fin_calculo"))]
candidatos.sort(key=lambda a: (comprables[a]["origen"] != "Financiación", nombre_de(a)))
if error_otc:
    st.caption("Ahora mismo no hemos podido revisar los proyectos en el mercado: solo aparecen los que se están "
               "financiando.")
if not candidatos:
    st.info("Ahora mismo no hay proyectos para calcular.")
    st.stop()
destino = st.selectbox(
    "Proyecto", candidatos, label_visibility="collapsed",
    format_func=lambda a: nombre_de(a) + (" · en financiación" if comprables[a]["origen"] == "Financiación"
                                          else " · en mercado"))
fila = por_addr[destino]
fin_destino = fila.get("fecha_fin_calculo")
T = meses_hasta(fin_destino)
divisa = "USD" if str(fila.get("Divisa", "EUR")).upper() in ("USD", "$", "USDT") else "EUR"
precio = comprables[destino]["precio"]           # emisión, o precio de mercado si se compra en OTC
precio_usd = precio * (usd_por_eur if divisa == "EUR" else 1.0)
rent = rentabilidad_ajustada(fila, estatus, precio)
st.caption(f"Termina en {mes_texto(fin_destino)} (unos {es(T)} meses) · precio por token "
           f"{es(precio, 2)} {'€' if divisa == 'EUR' else '$'}"
           f"{' (precio de mercado)' if comprables[destino]['origen'] == 'Mercado' else ''}"
           f" · rentabilidad total estimada con {estatus}: "
           f"{es(rent['total'], 1)} %"
           + (f" · [Dossier]({dossier_de(destino)})" if dossier_de(destino) else ""))
# Los tokens que se compran con el préstamo NO cuentan como garantía (sin «bucles» de pedir, comprar,
# depositar y volver a pedir)
cfg_nuevo = activos.get(destino) or {}
nuevo = {"garantia": False, "ltv": cfg_nuevo.get("ltv", 0.0), "umbral": cfg_nuevo.get("umbral", 0.0),
         "ratio": (precios.get(destino, precio_usd) / precio_usd) if precio_usd > 0 else 1.0}

# ---------- Resultado ----------
st.markdown("### ¿Cuánto puedes pedir?")
if not garantias:
    st.info("Elige arriba los proyectos de tu cartera para calcularlo." if modo != "Con mi wallet"
            else "Para simularlo necesitas tokens que sirvan de garantía.")
    st.stop()
with st.spinner("Consultando el tipo de interés de RNT Lend…"):
    apr, fuente_apr = _tipo_interes(clave)

res = calculo_lend.capacidad(garantias, deuda0, apr, T, nuevo)
_valor_g = sum(g["valor_usd"] for g in res["activas"])
ltv_medio = (sum(g["valor_usd"] * g["ltv"] for g in res["activas"]) / _valor_g) if _valor_g else 0.0
maximo = res["maximo"]
if res["vencidas"]:
    st.caption("No cuentan como garantía porque su fecha de fin ya ha pasado: "
               + ", ".join(g["nombre"] for g in res["vencidas"]) + ".")
if maximo < 50:
    terminan = [g["nombre"] for g in res["activas"] if g.get("meses_fin") is not None and g["meses_fin"] < T]
    if res["activas"] and len(terminan) == len(res["activas"]):
        lista = terminan[0] if len(terminan) == 1 else ", ".join(terminan[:-1]) + " y " + terminan[-1]
        st.warning(f"{lista} {'terminan' if len(terminan) > 1 else 'termina'} antes que {nombre_de(destino)}, así que "
                   f"dejaría{'n' if len(terminan) > 1 else ''} de contar como garantía antes de que acabe el préstamo. "
                   f"Prueba con un proyecto que termine antes o con inmuebles que terminen más tarde.")
    elif deuda0 > 0:
        st.warning("Con tu deuda actual y los intereses hasta el final del proyecto, ahora mismo no puedes pedir "
                   "más sin acercarte demasiado a la liquidación.")
    else:
        st.warning("Con estos inmuebles no se puede pedir una cantidad suficiente hasta el final de este proyecto.")
    st.stop()

st.markdown(tarjeta("Puedes pedir hasta", dos(maximo),
                    f"y no pagar nada hasta {mes_texto(fin_destino)}, "
                    f"cuando termina {nombre_de(destino)}", True), unsafe_allow_html=True)
st.write("")
# Cantidad a pedir: una caja grande donde escribirla y atajos al 50 %, 75 % y máximo
tope = int(maximo // 50 * 50)
if st.session_state.get("lend_tope") != tope:          # cambia el máximo (otra cartera u otro proyecto)
    st.session_state.lend_tope = tope
    st.session_state.importe_pedir = tope
st.session_state.importe_pedir = min(int(st.session_state.get("importe_pedir", tope)), tope)


def _fijar_importe(fraccion):
    st.session_state.importe_pedir = int(tope * fraccion // 50 * 50)


st.markdown(f"""
<style>
.st-key-importe_pedir input {{ font-size: 30px !important; font-weight: 800 !important; color: {NAVY} !important;
    height: 64px !important; }}
.st-key-importe_pedir [data-testid="stNumberInputContainer"] {{ border: 2px solid {AMBAR} !important;
    border-radius: 12px !important; background: #ffffff !important; height: 68px !important; }}
.st-key-importe_pedir [data-testid="stNumberInputContainer"] input {{ background: #ffffff !important; }}
</style>""", unsafe_allow_html=True)
st.markdown(f"<div style='font-size:22px; font-weight:800; color:{NAVY}; margin-top:6px;'>"
            f"¿Cuánto quieres pedir?</div><div style='color:{GRIS}; font-size:14px; margin-bottom:6px;'>"
            f"Escribe la cantidad en dólares (como máximo {es(tope)} $ · {es(tope / usd_por_eur)} €) o elige una "
            f"opción. El préstamo es en USDT.</div>",
            unsafe_allow_html=True)
c_imp, c_b1, c_b2, c_b3 = st.columns([2.2, 1, 1, 1], vertical_alignment="center")
prestamo = c_imp.number_input("Cantidad en $", min_value=0, max_value=tope, step=100, key="importe_pedir",
                              label_visibility="collapsed", format="%d")
c_b1.button("La mitad", use_container_width=True, on_click=_fijar_importe, args=(0.5,))
c_b2.button("El 75 %", use_container_width=True, on_click=_fijar_importe, args=(0.75,))
c_b3.button("El máximo", use_container_width=True, on_click=_fijar_importe, args=(1.0,))
st.caption(f"{es(prestamo)} $ ≈ {es(prestamo / usd_por_eur)} €")
tokens_compra = prestamo / precio_usd if precio_usd > 0 else 0
interes = calculo_lend.intereses(prestamo, apr, T)
devolver = prestamo + interes
# Lo que gana el proyecto hasta su final con el estatus del usuario: rentas (reinvertidas al cobrarlas,
# como en el simulador) y venta
# (el proyecto termina justo al final del plazo: así no se cuenta reinversión después de su venta)
fila_plazo = dict(fila)
fila_plazo["meses_hasta_fin"] = T
ganancia = ganancia_proyecto(prestamo, fila_plazo, estatus, precio, T)
neto = ganancia - interes
t1, t2, t3 = st.columns(3)
t1.markdown(tarjeta("Con lo que pides compras", f"{es(tokens_compra, 1)} tokens",
                    f"de {nombre_de(destino)} ({es(prestamo)} $ · {es(prestamo / usd_por_eur)} €)"),
            unsafe_allow_html=True)
t2.markdown(tarjeta("Al final devuelves", dos(devolver),
                    f"{es(prestamo)} $ del préstamo + {es(interes)} $ de intereses. Tipo: {es(apr * 100, 1)} % anual; "
                    f"en {es(T)} meses los intereses suman el {es(interes / prestamo * 100 if prestamo else 0, 1)} % "
                    f"de lo que pides (ya incluye intereses sobre intereses)"),
            unsafe_allow_html=True)
t3.markdown(tarjeta("Ganas de más", dos(neto, signo=True),
                    f"lo que gana el proyecto con tu estatus (~{es(ganancia)} $ · {es(ganancia / usd_por_eur)} €) "
                    f"menos los intereses", neto > 0), unsafe_allow_html=True)
if neto <= 0 and prestamo > 0:
    st.info(f"Con tu estatus ({estatus}) y {nombre_de(destino)}, ahora mismo no te compensa pedir financiación: "
            f"hasta {mes_texto(fin_destino)} los intereses suman un {es(interes / prestamo * 100)} % de lo que pides "
            f"y el proyecto te daría un {es(ganancia / prestamo * 100)} %. Prueba con otro proyecto"
            + (" o con un estatus superior." if estatus != "SuperReentel" else "."))

# Gráfico: la garantía de cada inmueble (bloques que desaparecen al vencer) y la deuda encima
puntos = calculo_lend.evolucion(garantias, deuda0, prestamo, apr, T, nuevo)
hoy = pd.Timestamp(date.today())
fecha_de = lambda m: hoy + pd.Timedelta(days=m * 30.4375)
validas_g = [g for g in res["activas"]]
antes = sorted([g for g in validas_g if g.get("meses_fin") is not None and g["meses_fin"] < T],
               key=lambda g: g["meses_fin"])
# Un color fijo por inmueble (máx. 6; el resto se agrupa en «Otros»). Sin naranja ni rojo, que son de la
# deuda y la liquidación.
COLORES_GARANTIA = ["#2a78d6", "#1baf7a", "#6250d6", "#e87ba4", "#008300", "#eda100", "#94a3b8"]
orden = sorted(validas_g, key=lambda g: -g["valor_usd"])
nombre_serie = {id(g): (g["nombre"] if k < 6 else "Otros") for k, g in enumerate(orden)}
series_g = list(dict.fromkeys(nombre_serie[id(g)] for g in orden))

largo, ancho = [], []
for m, pct, deuda_t, _ in puntos:
    final = abs(m - T) < 1e-9
    vivas = [g for g in validas_g if calculo_lend._viva(g, m, final)]
    fecha_m = fecha_de(m)
    por_serie = {n: 0.0 for n in series_g}
    for g in vivas:
        por_serie[nombre_serie[id(g)]] += g["valor_usd"]
    for n in series_g:
        largo.append({"Fecha": fecha_m, "Inmueble": n, "Valor": por_serie[n]})
    garantia_t = sum(por_serie.values())
    liq_t = sum(g["valor_usd"] * (g.get("umbral") or 0.8) for g in vivas)
    fila_a = {"Fecha": fecha_m, "Mes": fecha_m.strftime("%m/%Y"), "Deuda ($)": round(deuda_t),
              "Liquidación ($)": round(liq_t),
              # textos ya formateados en español para la ventana del ratón
              "Deuda": f"{es(deuda_t)} $ · {es(deuda_t / usd_por_eur)} €",
              "Garantía": f"{es(garantia_t)} $ · {es(garantia_t / usd_por_eur)} €",
              "Liquidación": f"{es(liq_t)} $",
              "Deuda sobre garantía": f"{es(pct, 1)} %" if pct is not None else "—",
              "Factor de salud": es(liq_t / deuda_t, 2) if deuda_t > 0 else "—"}
    for n in series_g:
        fila_a[f"g_{n}"] = f"{es(por_serie[n])} $" if por_serie[n] > 0 else "ya ha vencido"
    ancho.append(fila_a)
df_largo, df_ancho = pd.DataFrame(largo), pd.DataFrame(ancho)
datos_pct = [(m, p) for m, p, _, _ in puntos if p is not None]
pct_hoy = datos_pct[0][1] if datos_pct else None
pct_fin = datos_pct[-1][1] if datos_pct else None

eje_x = alt.X("Fecha:T", title=None, axis=alt.Axis(format="%m/%Y", tickCount=8, labelAngle=0, grid=False))
areas = alt.Chart(df_largo).mark_area(opacity=0.55, stroke="#ffffff", strokeWidth=2).encode(
    x=eje_x, y=alt.Y("Valor:Q", stack=True, title="Dólares ($)",
                     axis=alt.Axis(labelExpr="replace(format(datum.value, ',.0f'), /,/g, '.')")),
    color=alt.Color("Inmueble:N", sort=series_g,
                    scale=alt.Scale(domain=series_g, range=COLORES_GARANTIA[:len(series_g)]),
                    legend=alt.Legend(orient="bottom", title="Garantía por inmueble")))
linea_deuda = alt.Chart(df_ancho).mark_line(color=NAVY, strokeWidth=3).encode(x=eje_x, y="Deuda ($):Q")
linea_liq = alt.Chart(df_ancho).mark_line(color=ROJO, strokeWidth=2, strokeDash=[6, 4]).encode(
    x=eje_x, y="Liquidación ($):Q")
etiquetas = pd.DataFrame([
    {"Fecha": df_ancho["Fecha"].iloc[0], "y": df_ancho["Deuda ($)"].iloc[0], "t": "Tu deuda", "c": NAVY},
    {"Fecha": df_ancho["Fecha"].iloc[0], "y": df_ancho["Liquidación ($)"].iloc[0], "t": "Liquidación (80 %)",
     "c": ROJO}])
txt_lineas = alt.Chart(etiquetas).mark_text(align="left", dx=6, dy=-9, fontSize=12, fontWeight="bold").encode(
    x="Fecha:T", y="y:Q", text="t:N", color=alt.Color("c:N", scale=None))
# Al pasar el ratón: una línea vertical con todos los datos de esa fecha
cerca = alt.selection_point(nearest=True, on="pointermove", fields=["Fecha"], empty=False, clear="pointerout")
campos = ([alt.Tooltip("Mes:N", title="Fecha"), alt.Tooltip("Deuda:N", title="Tu deuda"),
           alt.Tooltip("Garantía:N", title="Tu garantía"), alt.Tooltip("Liquidación:N", title="Se liquida si la deuda llega a"),
           alt.Tooltip("Deuda sobre garantía:N"), alt.Tooltip("Factor de salud:N")]
          + [alt.Tooltip(f"g_{n}:N", title=n) for n in series_g])
vertical = alt.Chart(df_ancho).mark_rule(color=GRIS, strokeWidth=1).encode(
    x="Fecha:T", opacity=alt.condition(cerca, alt.value(0.9), alt.value(0)), tooltip=campos).add_params(cerca)
punto_deuda = alt.Chart(df_ancho).mark_point(color=NAVY, filled=True, size=60).encode(
    x="Fecha:T", y="Deuda ($):Q", opacity=alt.condition(cerca, alt.value(1), alt.value(0)))
capas = [areas, linea_liq, linea_deuda, txt_lineas, vertical, punto_deuda]
if antes:
    eventos = pd.DataFrame([{"Fecha": fecha_de(g["meses_fin"]), "t": f"Vence {g['nombre']}"} for g in antes])
    capas.append(alt.Chart(eventos).mark_text(color=GRIS, align="right", dx=-4, y=10, fontSize=11).encode(
        x="Fecha:T", text="t:N"))
st.write("")
st.markdown("**Cómo evoluciona tu deuda hasta el final** (con los intereses sumados, sin pagar nada)")
st.caption("Los bloques de colores son tu garantía, inmueble a inmueble: cuando uno vence, desaparece. La línea "
           "azul oscuro es tu deuda y la roja, el punto de liquidación. Pasa el ratón por el gráfico para ver los "
           "datos de cada fecha.")
st.altair_chart(alt.layer(*capas).properties(height=360), use_container_width=True)

if pct_hoy is not None:
    texto = (f"Al pedir este préstamo, tu deuda será el **{es(pct_hoy)} %** del valor de tu garantía (factor de "
             f"salud {es(80 / pct_hoy, 2)}). Como los intereses se van sumando, ese porcentaje subirá hasta el "
             f"**{es(pct_fin)} %** el último día (factor de salud {es(80 / pct_fin, 2)}). La liquidación se produce "
             f"si llega al 80 %.")
    if antes:
        nombres = [f"{g['nombre']} ({mes_texto(g['fecha_fin'])})" for g in antes]
        lista = nombres[0] if len(nombres) == 1 else ", ".join(nombres[:-1]) + " y " + nombres[-1]
        texto += (f"\n\nLos saltos del gráfico se deben a que {lista} "
                  f"{'terminan' if len(antes) > 1 else 'termina'} antes que {nombre_de(destino)}: a partir de "
                  f"ese momento, {'dejan' if len(antes) > 1 else 'deja'} de contar como garantía.")
    st.markdown(texto)
if pct_fin is not None and pct_fin > 76:
    st.warning("Con esta cantidad, el último día quedarías casi en el punto de liquidación: una bajada del valor de "
               "tu garantía o una subida del tipo de interés te liquidaría. Pide menos para tener margen.")

# Qué limita la cantidad máxima
lim = res["limita"]
if lim["motivo"] == "hoy":
    motivo = (f"Porque es lo máximo que RNT Lend permite pedir hoy con tu garantía: al pedir, solo deja llegar "
              f"hasta el {es(ltv_medio * 100)} % de su valor.")
elif lim["motivo"] == "fin_garantia":
    sale = " y ".join(lim["salen"])
    motivo = (f"Porque cuando termine {sale} dejará de contar como garantía y, desde ese día, tu deuda llegaría al "
              f"80 % de la garantía que te queda, que es el punto de liquidación.")
else:
    motivo = ("Porque, con los intereses acumulados, el último día tu deuda llegaría al 80 % de tu garantía, que es "
              "el punto de liquidación.")
st.markdown(f"**¿Por qué no puedes pedir más de {es(maximo)} $ ({es(maximo / usd_por_eur)} €)?** {motivo}")

# ---------- ¿Y si vas pagando los intereses? ----------
# Pagando los intereses cada cierto tiempo la deuda no se acumula y se puede pedir más. Para cada frecuencia:
# cuánto se puede pedir, cuánto se paga cada vez, los intereses totales y lo que se gana de más.
gana_por_dolar = ganancia_proyecto(1000.0, fila_plazo, estatus, precio, T) / 1000.0
frecuencias = [("Sin pagar hasta el final", None), ("Cada año", 12), ("Cada 6 meses", 6), ("Cada 3 meses", 3),
               ("Cada mes", 1)]
filas_f = []
for etiqueta, meses_p in frecuencias:
    if meses_p is not None and meses_p >= T:
        continue
    cap = calculo_lend.capacidad(garantias, deuda0, apr, T, nuevo, meses_p)["maximo"]
    cap = max(0.0, cap // 50 * 50)
    if meses_p is None:
        cuota, total_int = 0.0, calculo_lend.intereses(cap, apr, T)
    else:
        cuota, total_int = calculo_lend.intereses_pagando(cap, apr, T, meses_p)
    filas_f.append({"etiqueta": etiqueta, "max": cap, "cuota": cuota, "intereses": total_int,
                    "neto": cap * gana_por_dolar - total_int})
if len(filas_f) > 1:
    mejor_f = max(filas_f, key=lambda f: f["neto"])
    st.markdown("### ¿Y si vas pagando los intereses?")
    st.caption("Si pagas los intereses cada cierto tiempo, tu deuda no crece y puedes pedir más. Cada fila usa el "
               "máximo que podrías pedir con esa frecuencia. Los pagos los haces con tu dinero o con las rentas "
               "que vas cobrando.")
    cab = ("<tr style='background:#f3f4f6; color:#374151; font-size:13px;'>"
           "<th style='text-align:left; padding:8px 10px;'>Pago de intereses</th>"
           "<th style='text-align:right; padding:8px 10px;'>Puedes pedir hasta</th>"
           "<th style='text-align:right; padding:8px 10px;'>Pagas cada vez</th>"
           "<th style='text-align:right; padding:8px 10px;'>Intereses totales</th>"
           "<th style='text-align:right; padding:8px 10px;'>Ganas de más</th></tr>")
    cuerpo = ""
    for f in filas_f:
        optima = f is mejor_f and f["neto"] > 0
        estilo = f"background:#fff4de; font-weight:700;" if optima else "background:#ffffff;"
        cuerpo += (
            f"<tr style='{estilo} border-top:1px solid #e5e7eb; font-size:14px; color:{NAVY};'>"
            f"<td style='padding:8px 10px;'>{f['etiqueta']}"
            + (f" <span style='color:{AMBAR_OSCURO};'>· óptima</span>" if optima else "") + "</td>"
            f"<td style='text-align:right; padding:8px 10px;'>{es(f['max'])} $<br><span style='color:{GRIS}; "
            f"font-size:12px; font-weight:400;'>{es(f['max'] / usd_por_eur)} €</span></td>"
            f"<td style='text-align:right; padding:8px 10px;'>"
            + ("—" if not f["cuota"] else f"{es(f['cuota'])} $<br><span style='color:{GRIS}; font-size:12px; "
                                          f"font-weight:400;'>{es(f['cuota'] / usd_por_eur)} €</span>")
            + f"</td><td style='text-align:right; padding:8px 10px;'>{es(f['intereses'])} $<br><span "
              f"style='color:{GRIS}; font-size:12px; font-weight:400;'>{es(f['intereses'] / usd_por_eur)} €</span></td>"
            f"<td style='text-align:right; padding:8px 10px;'>{'+' if f['neto'] >= 0 else '−'}{es(abs(f['neto']))} $"
            f"<br><span style='color:{GRIS}; font-size:12px; font-weight:400;'>"
            f"{'+' if f['neto'] >= 0 else '−'}{es(abs(f['neto']) / usd_por_eur)} €</span></td></tr>")
    st.markdown(f"<div style='overflow-x:auto;'><table style='width:100%; border-collapse:collapse; border:1px "
                f"solid #e5e7eb; border-radius:10px; overflow:hidden;'>{cab}{cuerpo}</table></div>",
                unsafe_allow_html=True)
    if mejor_f["neto"] > 0:
        st.caption(f"La frecuencia óptima es la que más te hace ganar: **{mejor_f['etiqueta'].lower()}**.")
    else:
        st.caption("Con este proyecto y tu estatus, ninguna frecuencia compensa: los intereses superan lo que gana "
                   "el proyecto.")

with st.expander("Cómo se calcula"):
    st.markdown(
        f"- **Tipo de interés:** {es(apr * 100, 2)} % anual ({fuente_apr}). Es variable: si sube, la deuda crece "
        f"más rápido.\n"
        f"- **Intereses:** se suman a la deuda cada día y no se paga nada hasta el final.\n"
        f"- **Garantía:** el valor de tus tokens según RNT Lend, sin cambios en el tiempo. Si un inmueble termina "
        f"antes que el proyecto que compras, deja de contar ese día.\n"
        f"- **Límite:** la deuda nunca supera el 80 % del valor de tu garantía, que es donde se liquida (factor de "
        f"salud 1). Además, en el momento de pedir, RNT Lend solo te deja llegar hasta el {es(ltv_medio * 100)} % del "
        f"valor de tu garantía.\n"
        f"- **¿Cuánto ganas?** Tu ganancia se calcula considerando tu estatus ({estatus}), la reinversión de las "
        f"rentas que vas cobrando y el beneficio por la venta final del proyecto.\n"
        f"- Importes en dólares porque el préstamo es en USDT (1 € = {es(usd_por_eur, 4)} $).")
st.caption("Simulación informativa con estimaciones: no es asesoramiento ni una oferta de préstamo, y no garantiza "
           "resultados. Revisa siempre tu factor de salud en RNT Lend.")
