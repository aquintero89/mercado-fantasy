"""
Scraper diario del mercado de La Liga Fantasy (analiticafantasy.com)
=====================================================================

Qué hace:
- Abre la página del mercado con un navegador real (headless) porque la tabla
  usa paginación y pestañas "Subidas/Bajadas" controladas por JavaScript.
- Recorre las pestañas Subidas y Bajadas, y todas las páginas de cada una.
- Extrae: jugador, posición, equipo, precio, subida/bajada en € y en %.
- Guarda un CSV con la fecha del día: mercado_YYYY-MM-DD.csv
- Si ya existe un histórico.csv en la misma carpeta, le añade las filas del
  día (sin duplicar si ya corriste el script hoy).

Cómo usarlo:
    1. pip install playwright pandas requests --break-system-packages
    2. playwright install chromium
    3. Configura las variables TELEGRAM_TOKEN y TELEGRAM_CHAT_ID más abajo
       (o como variables de entorno) si quieres recibir el resumen por Telegram.
    4. python3 scrape_mercado.py

Cada día, simplemente vuelve a ejecutar el script. Va acumulando el
histórico en histórico.csv (para Google Sheets) y, si configuraste Telegram,
te envía un resumen al chat.
"""

import os
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import requests
from playwright.sync_api import sync_playwright

URL = "https://www.analiticafantasy.com/fantasy-la-liga/mercado"
OUT_DIR = Path(__file__).parent
POSITIONS = ["PT", "DF", "MC", "DL"]

# --- Configuración de Telegram (opcional) ---
# Pega aquí tu token y chat_id, o déjalo así y usa variables de entorno:
#   export TELEGRAM_TOKEN="123456:ABC..."
#   export TELEGRAM_CHAT_ID="987654321"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TOP_N = 10  # cuántos jugadores mostrar por categoría en el mensaje


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram no configurado (falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID). Omitido.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    if resp.status_code == 200:
        print("Resumen enviado a Telegram.")
    else:
        print(f"Error enviando a Telegram: {resp.status_code} {resp.text}")


def build_telegram_summary(df: pd.DataFrame) -> str:
    hoy = date.today().isoformat()
    lines = [f"<b>📊 Mercado Fantasy — {hoy}</b>", ""]

    subidas = df[df["tipo"] == "Subidas"].copy()
    if not subidas.empty:
        subidas["cambio_eur_num"] = pd.to_numeric(subidas["cambio_eur"], errors="coerce")
        subidas = subidas.sort_values("cambio_eur_num", ascending=False).head(TOP_N)
        lines.append("<b>🟢 Top subidas</b>")
        for _, r in subidas.iterrows():
            cambio = int(r["cambio_eur_num"]) if pd.notna(r["cambio_eur_num"]) else "?"
            lines.append(f"↑ {r['jugador']} ({r['equipo']}): +{cambio:,} € ({r['cambio_pct']}%)".replace(",", "."))
        lines.append("")

    bajadas = df[df["tipo"] == "Bajadas"].copy()
    if not bajadas.empty:
        bajadas["cambio_eur_num"] = pd.to_numeric(bajadas["cambio_eur"], errors="coerce")
        bajadas = bajadas.sort_values("cambio_eur_num", ascending=True).head(TOP_N)
        lines.append("<b>🔴 Top bajadas</b>")
        for _, r in bajadas.iterrows():
            cambio = int(r["cambio_eur_num"]) if pd.notna(r["cambio_eur_num"]) else "?"
            lines.append(f"↓ {r['jugador']} ({r['equipo']}): {cambio:,} € ({r['cambio_pct']}%)".replace(",", "."))

    return "\n".join(lines)


def split_jugador_cell(text: str):
    """La celda 'Jugador' viene como 'NombreDFEquipo' pegado.
    Buscamos el código de posición para separar nombre / posición / equipo."""
    for pos in POSITIONS:
        idx = text.find(pos)
        if idx != -1:
            nombre = text[:idx].strip()
            equipo = text[idx + len(pos):].strip()
            return nombre, pos, equipo
    return text.strip(), "", ""


def parse_change_cell(text: str):
    """'+2.645.168 €+3,6%' o '-993.000 €-2,1%' -> (valor_eur, pct)"""
    text = text.replace("\xa0", " ")
    m_val = re.search(r"([+-]?[\d.]+)\s*€", text)
    m_pct = re.search(r"([+-]?[\d,]+)\s*%", text)
    valor = m_val.group(1).replace(".", "") if m_val else None
    pct = m_pct.group(1).replace(",", ".") if m_pct else None
    return valor, pct


def parse_precio_cell(text: str):
    m = re.search(r"([\d.]+)\s*€", text)
    return m.group(1).replace(".", "") if m else None


def scrape_tab(page, tab_label: str, rows: list):
    """Hace clic en la pestaña (Subidas/Bajadas) y recorre todas las páginas."""
    try:
        page.get_by_text(tab_label, exact=True).click()
        page.wait_for_timeout(1500)
    except Exception:
        print(f"  Aviso: no encontré el botón '{tab_label}', sigo con la vista actual.")

    page_num = 1
    seen_first_row = None
    while True:
        page.wait_for_timeout(800)
        html = page.content()
        try:
            tables = pd.read_html(html)
        except ValueError:
            break

        table = max(tables, key=lambda t: t.shape[0])  # la tabla más grande de la página
        if table.empty:
            break

        first_row_sig = tuple(table.iloc[0].astype(str))
        if first_row_sig == seen_first_row:
            break  # no avanzó de página, evitamos loop infinito
        seen_first_row = first_row_sig

        for _, row in table.iterrows():
            jugador_raw = str(row.get("Jugador", ""))
            subida_raw = str(row.get("Subida", row.get("Bajada", "")))
            precio_raw = str(row.get("Precio", ""))

            nombre, posicion, equipo = split_jugador_cell(jugador_raw)
            valor, pct = parse_change_cell(subida_raw)
            precio = parse_precio_cell(precio_raw)

            if not nombre:
                continue

            rows.append({
                "fecha": date.today().isoformat(),
                "tipo": tab_label,
                "jugador": nombre,
                "posicion": posicion,
                "equipo": equipo,
                "precio_eur": precio,
                "cambio_eur": valor,
                "cambio_pct": pct,
            })

        print(f"  {tab_label} - página {page_num}: {len(table)} filas")

        siguiente = page.get_by_text("Siguiente", exact=True)
        if siguiente.count() == 0 or not siguiente.first.is_enabled():
            break
        siguiente.first.click()
        page_num += 1
        if page_num > 40:  # límite de seguridad
            break


def main():
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        print(f"Abriendo {URL} ...")
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        for tab in ["Subidas", "Bajadas"]:
            print(f"Extrayendo pestaña: {tab}")
            scrape_tab(page, tab, rows)

        browser.close()

    if not rows:
        print("No se extrajeron datos. La estructura de la web pudo haber cambiado.")
        sys.exit(1)

    df = pd.DataFrame(rows).drop_duplicates(subset=["fecha", "tipo", "jugador", "equipo"])
    today_file = OUT_DIR / f"mercado_{date.today().isoformat()}.csv"
    df.to_csv(today_file, index=False, encoding="utf-8-sig")
    print(f"\nGuardado: {today_file} ({len(df)} filas)")

    historico_file = OUT_DIR / "historico.csv"
    if historico_file.exists():
        hist = pd.read_csv(historico_file, dtype=str)
        hist = hist[hist["fecha"] != date.today().isoformat()]  # evita duplicar si re-ejecutas hoy
        combinado = pd.concat([hist, df], ignore_index=True)
    else:
        combinado = df
    combinado.to_csv(historico_file, index=False, encoding="utf-8-sig")
    print(f"Histórico actualizado: {historico_file} ({len(combinado)} filas totales)")
    print("\nAhora sube 'historico.csv' a Google Sheets (Archivo > Importar > Reemplazar datos,")
    print("o simplemente arrastra el archivo cada día y elige 'Reemplazar la hoja actual').")

    resumen = build_telegram_summary(df)
    send_telegram_message(resumen)


if __name__ == "__main__":
    main()
