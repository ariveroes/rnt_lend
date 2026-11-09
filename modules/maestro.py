"""
El maestro de inmuebles, leído una sola vez para toda la herramienta.

VERSIÓN CON GSPREAD: Lee via Google Sheets API (auténticado).
No usa URL pública, usa credenciales de Streamlit secrets.

POR QUÉ ESTÁ AQUÍ
----------------
La hoja maestra es la fuente de verdad de todo lo que no vive en la cadena:
el nombre del proyecto, dónde está, bajo qué emisión se tokenizó, qué
rentabilidad se estimó para cada estatus y —en los cerrados— cuál acabó
siendo la real.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

# Constantes
GSHEET_ID = "1sL6fynVPKtfaNs22t019ItKbzMFaOHbO5kqVzMxXHIY"
WORKSHEET_NAME = "Master Inmuebles Pro"

# nombre de cabecera → posición de reserva (0-based) por si la cabecera cambia
COLUMNAS = {
    "id":                    ("ID", 0),
    "nombre":                ("Nombre del proyecto", 1),
    "estado":                ("ESTADO", 2),
    "lanzamiento":           ("LANZAMIENTO", 3),
    "fecha_financiacion":    ("Fecha de Financiación", 6),
    "fecha_inicio_renta":    ("Estimación fecha Inicio de Proyecto desde Financiación", 7),
    "fecha_fin_estimada":    ("Estimación fecha fin desde Financiación", 8),
    "tokens_emitidos":       ("Nº de Tokens", 9),
    "precio_emision":        ("Px Emisión  Token", 10),
    "divisa_raw":            ("Divisa", 11),
    "ubicacion":             ("Ubicación", 14),
    "tipologia_explotacion": ("Tipología de explotación", 15),
    "tipologia_dividendo":   ("Tipología de Dividendo", 16),
    "emision":               ("Emisión de Tokenización", 17),
    "address":               ("Token Address", 18),
    "colateralizable":       ("Permite colateralización", 21),
    # Rentabilidades ESTIMADAS por estatus
    "est_total_rnt":         ("Estimación Rentab. Total Reentel", 22),
    "est_recurr_rnt":        ("Estimación Rentab. Rendim. Recurr. anualizados Reentel", 23),
    "est_plusvalia_rnt":     ("Estimación Rentab. Plusvalía Reentel", 24),
    "est_anual_rnt":         (None, 25),
    "est_total_rp":          ("Estimación Rentab. Total RP", 26),
    "est_recurr_rp":         ("Estimación Rentab. Rendim. Recurr. anualizados RP", 27),
    "est_plusvalia_rp":      ("Estimación Rentab. Plusvalía RP", 28),
    "est_anual_rp":          (None, 29),
    "est_total_sr":          ("Estimación Rentab. Total SR", 30),
    "est_recurr_sr":         ("Estimación Rentab. Rendim. Recurr. anualizados SR", 31),
    "est_plusvalia_sr":      ("Estimación Rentab. Plusvalía SR", 32),
    "est_anual_sr":          (None, 33),
    "meses_pendientes":      ("Estimación Nº Meses pendientes de renta hasta Estimación fin", 44),
    "meses_en_curso":        ("Real Nº Meses en curso en rentabilidad", 43),
    # Meses hasta el cierre del proyecto (columna DB). Solo por nombre: si la
    # cabecera no existe, se deja vacío en vez de leer otra columna por posición.
    "meses_hasta_fin":       ("Meses hasta el fin", 10**6),
    # Cierre real: solo lo tienen los proyectos CERRADOS
    "fecha_fin_real":        ("Real fecha de fin", 55),
    # Fecha de fin reestimada para los proyectos que ya han sobrepasado la
    # original (columna CA). Es la que manda para saber cuándo termina de verdad.
    "fecha_fin_auxiliar":    ("Fecha fin Estimada Auxiliar para proyectos q la han sobrepasado", 78),
    "real_total_rnt":        ("Real Rentab. Total Reentel", 59),
    "real_anual_rnt":        ("Real Rentab. Total Anualizada Reentel", 61),
    "real_total_sr":         ("Real Rentab. Total SR", 69),
    "real_anual_sr":         ("Real Rentab. Total Anualizada SR", 71),
    # Renta recurrente REAL anualizada (la que ya está pagando el proyecto) y
    # plusvalía REAL (solo cerrados). Las usa rentabilidad_compra.py, igual que
    # la plantilla de propuestas: si hay dato real manda, si no, el estimado.
    "real_recurr_rnt":       ("Real Rentab. por Rendimientos recurrentes Anualizados Reentel", 53),
    "real_recurr_rp":        ("Real Rentab. por Rendimientos recurrentes Anualizados RP", 83),
    "real_recurr_sr":        ("Real Rentab. por Rendimientos recurrentes Anualizados SR", 87),
    "real_plusvalia_rnt":    ("Real Rentab. Plusvalía Reentel", 60),
    "real_plusvalia_rp":     ("Real Rentab. Plusvalía RP", 65),
    "real_plusvalia_sr":     ("Real Rentab. Plusvalía SR", 70),
    "descripcion":           ("Descripción", 74),
    "link_web":              ("Link a inmueble en la web publica", 75),
    "motivo_cierre":         ("Motivos de cierre Estimado vs Real", 80),
    "dossier":               ("Link a Dossier Comercial", 94),
    "whitepaper":            ("Link a Whitepaper en Español", 95),
}

ABIERTOS = ("EN EXPLOTACIÓN", "EN REFORMA", "EN CONSTRUCCIÓN", "FINANCIÁNDOSE", "PRELANZAMIENTO")

# Las tres categorías, con el sufijo que usan las columnas del maestro.
ESTATUS = (("Reentel", "rnt"), ("ReentelPro", "rp"), ("SuperReentel", "sr"))


# ─── Normalización ───────────────────────────────────────────────────────────

def texto(valor) -> str:
    s = str(valor).strip()
    return "" if s.lower() in ("nan", "none", "-", "no hay") else s


def numero(valor):
    """Un número del maestro, venga como '28,94%', '1.234,56' o ya numérico."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return None if valor != valor else float(valor)      # NaN -> None
    s = str(valor).strip().replace("%", "").replace(" ", "")
    if not s or s.lower() in ("nan", "none", "-", "cerrado"):
        return None
    # Miles con coma y decimales con punto (formato anglosajón de la hoja),
    # o al revés. Se decide por cuál aparece el último.
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def porcentaje(valor):
    """Un porcentaje del maestro, siempre en unidades de % (28.94 = 28,94 %).

    En el maestro conviven dos formas de escribirlo: con símbolo ("28,94%") y
    como fracción sin símbolo ("0.2894"). Sin el símbolo, un valor de 1 o menos
    se entiende como fracción y se pasa a %; si no, la rentabilidad salía cien
    veces más pequeña (p. ej. Rentas 3 aparecía con un 0,10 % en vez de 13,91 %).
    """
    n = numero(valor)
    if n is None:
        return None
    # El Sheet se lee con los valores SIN formato: los porcentajes llegan
    # siempre como fracción (0.1391 = 13,91 %), sea cual sea su formato de celda.
    if isinstance(valor, (int, float)):
        return n * 100
    if "%" not in str(valor) and abs(n) <= 1:
        return n * 100
    return n


def fecha(valor):
    """Una fecha del maestro, probando los formatos que aparecen en ella."""
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        # Fecha como número de serie de Google Sheets (días desde 30/12/1899)
        return (date(1899, 12, 30) + timedelta(days=int(valor))) if valor > 0 else None
    s = texto(valor)
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ─── Lectura con gspread ─────────────────────────────────────────────────────

def _obtener_cliente_gspread():
    """Autentica con Google Sheets usando Streamlit secrets."""
    try:
        creds_dict = st.secrets.get("gcp_service_account")
        if not creds_dict:
            print("❌ No se encontraron credenciales [gcp_service_account] en Streamlit secrets")
            return None
        
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=[
                'https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive'
            ]
        )
        
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        print(f"❌ Error autenticando con Google Sheets: {e}")
        return None


@st.cache_data(ttl=3600)
def _obtener_datos_sheet():
    """Obtiene todos los datos del Sheet via gspread."""
    client = _obtener_cliente_gspread()
    if not client:
        return []
    
    try:
        sheet = client.open_by_key(GSHEET_ID)
        worksheet = sheet.worksheet(WORKSHEET_NAME)
        
        # Obtener TODAS las filas, con los números SIN formato. Con el valor
        # formateado se perdían decimales: una rentabilidad de 13,91 % en una
        # celda con formato "0.0" llegaba como "0.1" (así salía Rentas 3 mal).
        # Las fechas sí se piden como texto (dd/mm/aaaa).
        datos = worksheet.get_all_values(value_render_option="UNFORMATTED_VALUE",
                                         date_time_render_option="FORMATTED_STRING")
        
        if not datos:
            print("⚠️ El Master está vacío")
            return []
        
        return datos
    except Exception as e:
        print(f"❌ Error leyendo el Master: {e}")
        return []


def _valor(row, cabeceras: dict, clave: str):
    """Obtiene valor de una fila por cabecera o índice fallback."""
    nombre, pos = COLUMNAS[clave]
    if nombre and nombre in cabeceras:
        try:
            return row[cabeceras[nombre]]
        except (IndexError, KeyError):
            pass
    try:
        return row[pos] if pos < len(row) else ""
    except (IndexError, KeyError):
        return ""


def proyectos() -> list:
    """Todos los proyectos del maestro, ya normalizados."""
    datos = _obtener_datos_sheet()
    if not datos or len(datos) < 3:
        return []
    
    # Encontrar la fila de cabeceras (busca "Token Address")
    fila_cab = None
    for i, row in enumerate(datos):
        if any("Token Address" in str(v) for v in row):
            fila_cab = i
            break
    
    if fila_cab is None:
        return []
    
    # Mapear cabeceras
    cabeceras = {}
    for i, v in enumerate(datos[fila_cab]):
        nombre = str(v).strip()
        if nombre and nombre not in cabeceras:
            cabeceras[nombre] = i
    
    filas = datos[fila_cab + 1:]
    
    salida = []
    for row in filas:
        addr = texto(_valor(row, cabeceras, "address")).lower()
        if not addr.startswith("0x") or len(addr) != 42:
            continue
        
        divisa_raw = texto(_valor(row, cabeceras, "divisa_raw"))
        p = {
            "address": addr,
            "divisa": "USD" if "$" in divisa_raw else "EUR",
            "precio_emision": numero(_valor(row, cabeceras, "precio_emision")),
            "tokens_emitidos": numero(_valor(row, cabeceras, "tokens_emitidos")),
            "meses_pendientes": numero(_valor(row, cabeceras, "meses_pendientes")),
            "meses_en_curso": numero(_valor(row, cabeceras, "meses_en_curso")),
            "meses_hasta_fin": numero(_valor(row, cabeceras, "meses_hasta_fin")),
        }
        
        for clave in ("id", "nombre", "estado", "ubicacion", "tipologia_explotacion",
                      "tipologia_dividendo", "emision", "colateralizable",
                      "descripcion", "link_web", "motivo_cierre", "dossier", "whitepaper"):
            p[clave] = texto(_valor(row, cabeceras, clave))
        
        for clave in ("lanzamiento", "fecha_financiacion", "fecha_inicio_renta",
                      "fecha_fin_estimada", "fecha_fin_real", "fecha_fin_auxiliar"):
            p[clave] = fecha(_valor(row, cabeceras, clave))
        # Fecha en la que termina el proyecto, con la misma prioridad que la
        # plantilla de propuestas: la auxiliar (CA), la real y, si no, la estimada.
        p["fecha_fin_calculo"] = (p["fecha_fin_auxiliar"] or p["fecha_fin_real"]
                                  or p["fecha_fin_estimada"])
        
        for clave in COLUMNAS:
            if clave.startswith(("est_", "real_")):
                p[clave] = porcentaje(_valor(row, cabeceras, clave))
        
        p["label"] = p["id"] or p["nombre"] or addr[:10]
        p["abierto"] = p["estado"].upper() in ABIERTOS
        salida.append(p)
    
    return salida


def por_direccion(lista: list) -> dict:
    return {p["address"]: p for p in lista}


def rentabilidad(proy: dict, estatus_sufijo: str, campo: str = "anual"):
    """Rentabilidad estimada de un proyecto para un estatus.

    `campo` es 'anual' (total anualizada), 'total', 'recurr' o 'plusvalia'.
    Si el proyecto no diferencia por estatus —los anteriores al programa
    repiten la misma cifra en los tres— devuelve esa, que es lo correcto.
    """
    v = proy.get(f"est_{campo}_{estatus_sufijo}")
    return v if v is not None else proy.get(f"est_{campo}_rnt")
