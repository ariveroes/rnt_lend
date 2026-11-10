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

from modules import aave_lend, calculo_lend, data_loader
from modules.rentabilidad_compra import TASA_REINVERSION, ganancia_proyecto, rentabilidad_ajustada

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
st.caption("Revisa tu situación actual en RNT Lend o simula cuánta financiación extra podrías pedir usando tus "
           "tokens como garantía, sin pagar nada hasta que termine el proyecto en el que inviertas.")

clave = api_key()
with st.spinner("Leyendo los proyectos y RNT Lend…"):
    df = data_loader.cargar_todos_proyectos()
    activos, precios = _pool(clave)
    usd_por_eur = _usd_por_eur()
if df is None or df.empty or not activos:
    st.error("Ahora mismo no podemos leer los proyectos o RNT Lend. Prueba de nuevo en unos minutos.")
    st.stop()

por_addr = {str(r.get("Token Address", "") or "").strip().lower(): r for _, r in df.iterrows()
            if str(r.get("Token Address", "") or "").startswith("0x")}


def nombre_de(addr):
    r = por_addr.get(addr)
    return str(r.get("Nombre del proyecto")) if r is not None else addr[:10] + "…"


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
                ["Con mi wallet", "Sin wallet: elijo mis inmuebles y tokens"], horizontal=True)

garantias, deuda0, tokens, rnt_estatus = [], 0.0, {}, 0.0
if modo == "Con mi wallet":
    st.markdown("## Tu situación actual")
    c1, c2 = st.columns([3, 1])
    wallet = c1.text_input("Dirección de tu wallet", placeholder="0x…", label_visibility="collapsed").strip()
    if c2.button("Ver mi posición", type="primary", use_container_width=True):
        if not (wallet.startswith("0x") and len(wallet) == 42):
            st.error("La dirección no parece válida: empieza por 0x y tiene 42 caracteres.")
        else:
            with st.spinner("Leyendo tu posición en RNT Lend… (puede tardar unos segundos)"):
                st.session_state.lend_wallet = (wallet.lower(),) + _leer_wallet(
                    wallet.lower(), clave, tuple(sorted(activos.items())))
    datos = st.session_state.get("lend_wallet")
    if not datos:
        st.stop()
    _, cuenta, tokens, rnt_estatus = datos
    if not cuenta:
        st.warning("No hemos podido leer tu posición en RNT Lend. Prueba de nuevo en unos minutos.")
    cuenta = cuenta or {}
    deuda0 = cuenta.get("deuda_usd", 0.0)
    colateral = cuenta.get("colateral_usd", 0.0)
    hf = cuenta.get("health_factor")
    k1, k2, k3 = st.columns(3)
    k1.markdown(tarjeta("¿Cuánto tengo ya aportado como garantía en RNT Lend?", f"{es(colateral)} $",
                        f"≈ {es(colateral / usd_por_eur)} €", alto=True), unsafe_allow_html=True)
    k2.markdown(tarjeta("¿A cuánto asciende mi deuda actualmente?", f"{es(deuda0)} $",
                        "Lo que pediste más los intereses acumulados", alto=True), unsafe_allow_html=True)
    k3.markdown(tarjeta("¿Cuál es mi factor de salud?", es(hf, 2) if hf else "Sin deuda",
                        "Se liquida si baja de 1, es decir, cuando tu deuda llega al 80&nbsp;% de tu garantía."
                        if hf else "Sin riesgo de liquidación.", alto=True, color=color_hf(hf)),
                unsafe_allow_html=True)
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
    st.markdown("## Tu cartera")
    opciones = sorted([a for a in activos if sirve_de_garantia(a) and a in por_addr
                       and "CERRAD" not in str(por_addr[a].get("ESTADO", "")).upper()], key=nombre_de)
    base = pd.DataFrame({"Proyecto": [nombre_de(a) for a in opciones], "Tokens": [0.0] * len(opciones)})
    st.caption("Escribe cuántos tokens tienes (o tendrías) de cada inmueble. Solo aparecen los que se pueden "
               "usar como garantía en RNT Lend.")
    editado = st.data_editor(base, hide_index=True, use_container_width=True, key="tokens_manual",
                             disabled=["Proyecto"],
                             column_config={"Tokens": st.column_config.NumberColumn(min_value=0.0, step=1.0)})
    for addr, n in zip(opciones, editado["Tokens"]):
        if _num(n) > 0:
            garantias.append(garantia_de(addr, _num(n)))
    deuda0 = st.number_input("¿Ya tienes deuda en RNT Lend? (en $)", min_value=0.0, value=0.0, step=100.0)

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
            base = pd.DataFrame({"Proyecto": [nombre_de(a) for a in sueltos],
                                 "Disponibles en tu wallet": [round(tokens[a]["wallet"], 2) for a in sueltos],
                                 "Añadir el 100 %": [True] * len(sueltos),
                                 col_escribe: [None] * len(sueltos)})
            editado = st.data_editor(
                base, hide_index=True, use_container_width=True, key="tokens_anadir",
                disabled=["Proyecto", "Disponibles en tu wallet"],
                column_config={
                    "Disponibles en tu wallet": st.column_config.NumberColumn(format="%.2f"),
                    "Añadir el 100 %": st.column_config.CheckboxColumn(),
                    col_escribe: st.column_config.NumberColumn(min_value=0.0, step=1.0, format="%.2f")})
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
                           f"garantía, que son los que tienes disponibles. Usamos esa cantidad en el cálculo.")
    for addr, t in tokens.items():
        n = t["garantia"] + anadir.get(addr, 0.0)
        if sirve_de_garantia(addr) and n > 0:
            garantias.append(garantia_de(addr, n))

detectado = ("SuperReentel" if rnt_estatus >= RNT_POR_ESTATUS["SuperReentel"]
             else "ReentelPro" if rnt_estatus >= RNT_POR_ESTATUS["ReentelPro"] else "Reentel")
st.markdown("**Tu estatus**")
estatus = st.selectbox(
    "Tu estatus", ESTATUS, index=ESTATUS.index(detectado), label_visibility="collapsed",
    help=("Lo hemos detectado por los RNT que tienes en estatus en tu wallet; cámbialo si no es correcto."
          if modo == "Con mi wallet" else "Cambia la rentabilidad del proyecto y la de lo que reinviertes."))
st.markdown("**¿En qué proyecto quieres invertir?**")
candidatos = []
for addr, r in por_addr.items():
    fin = r.get("fecha_fin_calculo")
    m = meses_hasta(fin)
    if "CERRAD" in str(r.get("ESTADO", "")).upper() or m is None or m < MESES_MINIMOS:
        continue
    if _num(r.get("Precio Emisión")) <= 0:
        continue
    candidatos.append(addr)
candidatos.sort(key=lambda a: (str(por_addr[a].get("ESTADO", "")).upper() != "FINANCIÁNDOSE", nombre_de(a)))
if not candidatos:
    st.info("Ahora mismo no hay proyectos para calcular.")
    st.stop()
destino = st.selectbox(
    "Proyecto", candidatos, label_visibility="collapsed",
    format_func=lambda a: nombre_de(a) + (" · en financiación"
                                          if str(por_addr[a].get("ESTADO", "")).upper() == "FINANCIÁNDOSE" else ""))
fila = por_addr[destino]
fin_destino = fila.get("fecha_fin_calculo")
T = meses_hasta(fin_destino)
divisa = "USD" if str(fila.get("Divisa", "EUR")).upper() in ("USD", "$", "USDT") else "EUR"
precio = _num(fila.get("Precio Emisión"), 100.0)
precio_usd = precio * (usd_por_eur if divisa == "EUR" else 1.0)
rent = rentabilidad_ajustada(fila, estatus)
st.caption(f"Termina en {mes_texto(fin_destino)} (unos {es(T)} meses) · precio por token "
           f"{es(precio, 2)} {'€' if divisa == 'EUR' else '$'} · rentabilidad total estimada con {estatus}: "
           f"{es(rent['total'], 1)} %")
# Los tokens que se compran con el préstamo NO cuentan como garantía (sin «bucles» de pedir, comprar,
# depositar y volver a pedir)
cfg_nuevo = activos.get(destino) or {}
nuevo = {"garantia": False, "ltv": cfg_nuevo.get("ltv", 0.0), "umbral": cfg_nuevo.get("umbral", 0.0),
         "ratio": (precios.get(destino, precio_usd) / precio_usd) if precio_usd > 0 else 1.0}

# ---------- Resultado ----------
st.markdown("### ¿Cuánto puedes pedir?")
if not garantias:
    st.info("Indica tus inmuebles arriba para calcularlo." if modo != "Con mi wallet"
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
    if deuda0 > 0:
        st.warning("Con tu deuda actual y los intereses hasta el final del proyecto, ahora mismo no puedes pedir "
                   "más sin acercarte demasiado a la liquidación.")
    else:
        st.warning("Con estos inmuebles no se puede pedir una cantidad suficiente hasta el final de este proyecto.")
    st.stop()

st.markdown(tarjeta("Puedes pedir hasta", f"{es(maximo)} $",
                    f"≈ {es(maximo / usd_por_eur)} € · y no pagar nada hasta {mes_texto(fin_destino)}, "
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
            f"Escribe la cantidad en dólares (como máximo {es(tope)} $) o elige una opción.</div>",
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
ganancia = ganancia_proyecto(prestamo, fila_plazo, estatus, None, T)
neto = ganancia - interes
t1, t2, t3 = st.columns(3)
t1.markdown(tarjeta("Con lo que pides compras", f"{es(tokens_compra, 1)} tokens",
                    f"de {nombre_de(destino)} ({es(prestamo)} $)"), unsafe_allow_html=True)
t2.markdown(tarjeta("Al final devuelves", f"{es(devolver)} $",
                    f"{es(prestamo)} $ del préstamo + {es(interes)} $ de intereses ({es(apr * 100, 1)} % anual)"),
            unsafe_allow_html=True)
t3.markdown(tarjeta("Ganas de más", f"{'+' if neto >= 0 else '−'}{es(abs(neto))} $",
                    f"lo que gana el proyecto con tu estatus (~{es(ganancia)} $) menos los intereses", neto > 0),
            unsafe_allow_html=True)
if neto <= 0 and prestamo > 0:
    st.info(f"Con tu estatus ({estatus}) y {nombre_de(destino)}, ahora mismo no te compensa pedir financiación: "
            f"hasta {mes_texto(fin_destino)} los intereses suman un {es(interes / prestamo * 100)} % de lo que pides "
            f"y el proyecto te daría un {es(ganancia / prestamo * 100)} %. Prueba con otro proyecto"
            + (" o con un estatus superior." if estatus != "SuperReentel" else "."))

# Gráfico: deuda sobre garantía hasta el final
puntos = calculo_lend.evolucion(garantias, deuda0, prestamo, apr, T, nuevo)
hoy = pd.Timestamp(date.today())
fecha_de = lambda m: hoy + pd.Timedelta(days=m * 30.4375)
datos_g = pd.DataFrame([{"Fecha": fecha_de(m), "Deuda": p} for m, p, _, _ in puntos if p is not None])
pct_hoy = datos_g["Deuda"].iloc[0] if len(datos_g) else None
pct_fin = datos_g["Deuda"].iloc[-1] if len(datos_g) else None
antes = sorted([g for g in res["activas"] if g.get("meses_fin") is not None and g["meses_fin"] < T],
               key=lambda g: g["meses_fin"])

eje_x = alt.X("Fecha:T", title=None, axis=alt.Axis(format="%m/%Y", tickCount=8, labelAngle=0, grid=False))
base_g = alt.Chart(datos_g).encode(x=eje_x)
area = base_g.mark_area(color=AMBAR, opacity=0.18).encode(
    y=alt.Y("Deuda:Q", title="Deuda sobre garantía (%)", scale=alt.Scale(domain=[0, 95])))
linea = base_g.mark_line(color=NAVY, strokeWidth=3).encode(
    y="Deuda:Q", tooltip=[alt.Tooltip("Fecha:T", format="%m/%Y", title="Fecha"),
                          alt.Tooltip("Deuda:Q", format=".1f", title="Deuda sobre garantía (%)")])
liquidacion = alt.Chart(pd.DataFrame({"y": [80.0]})).mark_rule(color=ROJO, strokeDash=[6, 4], strokeWidth=2).encode(y="y:Q")
txt_liq = alt.Chart(pd.DataFrame({"x": [datos_g["Fecha"].iloc[0]], "y": [80.0],
                                  "t": ["Liquidación: 80 %"]})).mark_text(
    color=ROJO, align="left", dx=4, dy=-8, fontSize=12, fontWeight="bold").encode(x="x:T", y="y:Q", text="t:N")
extremos = pd.DataFrame([{"Fecha": datos_g["Fecha"].iloc[0], "Deuda": pct_hoy, "t": f"Hoy: {es(pct_hoy)} %",
                          "a": "left"},
                         {"Fecha": datos_g["Fecha"].iloc[-1], "Deuda": pct_fin, "t": f"Final: {es(pct_fin)} %",
                          "a": "right"}])
puntos_ext = alt.Chart(extremos).mark_point(color=NAVY, filled=True, size=70).encode(x="Fecha:T", y="Deuda:Q")
txt_ini = alt.Chart(extremos.iloc[:1]).mark_text(color=NAVY, align="left", dx=6, dy=-12, fontSize=13,
                                                 fontWeight="bold").encode(x="Fecha:T", y="Deuda:Q", text="t:N")
txt_fin = alt.Chart(extremos.iloc[1:]).mark_text(color=NAVY, align="right", dx=-6, dy=-12, fontSize=13,
                                                 fontWeight="bold").encode(x="Fecha:T", y="Deuda:Q", text="t:N")
capas = [area, linea, liquidacion, txt_liq, puntos_ext, txt_ini, txt_fin]
if antes:
    eventos = pd.DataFrame([{"Fecha": fecha_de(g["meses_fin"]), "t": f"Termina {g['nombre']}"} for g in antes])
    capas.append(alt.Chart(eventos).mark_rule(color=GRIS, strokeDash=[2, 3]).encode(x="Fecha:T"))
    capas.append(alt.Chart(eventos).mark_text(color=GRIS, align="right", dx=-4, y=12, fontSize=11).encode(
        x="Fecha:T", text="t:N"))
st.write("")
st.markdown("**Cómo evoluciona tu deuda hasta el final** (con los intereses sumados, sin pagar nada)")
st.altair_chart(alt.layer(*capas).properties(height=320), use_container_width=True)

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
st.markdown(f"**¿Por qué no puedes pedir más de {es(maximo)} $?** {motivo}")

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
