from playwright.sync_api import sync_playwright

# Dirección de cada app pública (copiada del navegador), una por línea.
APPS = [
    "https://reental-simuladordecartera.streamlit.app/",
    "https://reental-rntlend.streamlit.app/",
]

BOTON_DESPERTAR = "Yes, get this app back up!"


def main():
    with sync_playwright() as p:
        navegador = p.chromium.launch()
        for url in APPS:
            pagina = navegador.new_page()
            try:
                pagina.goto(url, timeout=120_000)
                pagina.wait_for_timeout(8_000)
                boton = pagina.get_by_role("button", name=BOTON_DESPERTAR)
                if boton.count():
                    boton.first.click()
                    print(f"DESPERTADA  {url}")
                else:
                    print(f"YA DESPIERTA {url}")
                pagina.wait_for_timeout(90_000)   # que cargue y guarde sus datos
            except Exception as e:
                print(f"ERROR {url}: {e}")
            finally:
                pagina.close()
        navegador.close()


if __name__ == "__main__":
    main()
