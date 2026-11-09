# ¿Cuánto puedo pedir en RNT Lend? (herramienta pública)

El usuario pone su wallet (o elige a mano sus inmuebles y tokens) y el proyecto en el
que quiere invertir. La herramienta calcula cuánto puede pedir hoy en RNT Lend, con sus
inmuebles como garantía, para no pagar nada hasta que ese proyecto termine, sin que la
deuda pase nunca del 78 % de la garantía (la liquidación es al 80 %).

## Cómo publicarla (Streamlit Community Cloud)
1. Crea un repositorio en GitHub (por ejemplo `rnt_lend`) y sube todo el contenido de
   esta carpeta, incluida la carpeta oculta `.streamlit`.
2. En share.streamlit.io → *Create app* → elige ese repositorio y `app.py`.
3. En *Advanced settings → Secrets* pega los mismos datos que en el simulador:

```toml
[gcp_service_account]
# … igual que en el simulador …

[etherscan]
api_key = "…"
```

## Supuestos del cálculo (modules/calculo_lend.py)
- Los intereses se suman a la deuda de forma continua y no se paga nada hasta el final.
- El tipo de interés es la media histórica del préstamo en USDT de RNT Lend.
- La garantía vale lo que dice el oráculo de RNT Lend y no cambia.
- La deuda nunca pasa del 78 % de la garantía; hoy, además, no se supera el LTV de cada activo.
- Si un inmueble de la garantía termina antes que el proyecto comprado, deja de contar.
- Los tokens comprados con el préstamo cuentan como garantía si el proyecto se puede colateralizar.
