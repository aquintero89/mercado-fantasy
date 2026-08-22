"""
Scraper diario del mercado de La Liga Fantasy (analiticafantasy.com)
=====================================================================

Qué hace:
- Abre la página del mercado con un navegador real (headless) porque la tabla
  usa paginación controlada por JavaScript.
- Recorre todas las páginas del mercado completo (subidas y bajadas incluidas
  en la misma tabla).
- Extrae: jugador, posición, equipo, precio, subida/bajada en € y en %.
- Guarda un CSV con la fecha del día: mercado_YYYY-MM-DD.csv
- Actualiza histórico.csv (mercado completo) e histórico_mi_equipo.csv
  (filtrado a los jugadores de mis_jugadores.txt).
- Envía un resumen a Telegram si TELEGRAM_TOKEN y TELEGRAM_CHAT_ID están
  configurados.

Cómo usarlo:
    1. pip install playwright pandas requests lxml --break-system-packages
    2. playwright install chromium
    3. Configura las variables TELEGRAM_TOKEN y TELEGRAM_CHAT_ID más abajo
       (o como variables de entorno) si quieres recibir el resumen por Telegram.
    4. python3 scrape_mercado.py

Cada día, simplemente vuelve a ejecutar el script. Va acumulando el
histórico en histórico.csv (para Google Sheets) y, si configuraste Telegram,
te envía un resumen al chat.
"""

import io
import os
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

import pandas as pd
import requests
from playwright.sync_api import sync_playwright

URL = "https://www.analiticafantasy.com/fantasy-la-liga/mercado"
OUT_DIR = Path(__file__).parent
POSITIONS = ["PT", "DF", "MC", "DL", "DT"]  # DT = entrenador

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TOP_N = 10

MIS_JUGADORES_FILE = OUT_DIR / "mis_jugadores.txt"


def normaliza(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return texto.lower().strip()


def carga_mis_jugadores() -> list:
    if not MIS_JUGADORES_FILE.exists():
        return []
    with open(MIS_JUGADORES_FILE, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def filtra_mi_equipo(df: pd.DataFrame, nombres: list) -> pd.DataFrame:
    if not nombres:
        return df.iloc[0:0]
    nombres_norm = [normaliza(n) for n in nombres]
    jugador_norm = df["jugador"].apply(normaliza)
    mask = jugador_norm.apply(lambda j: any(n in j or j in n for n in nombres_norm))
    return df[mask]


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


def build_telegram_summary_mi_equipo(df: pd.DataFrame, encontrados: int, total: int) -> str:
    hoy = date.today().isoformat()
    lines = [f"<b>⚽ Mi equipo — {hoy}</b> ({encontrados}/{total} encontrados)", ""]
    df = df.copy()
    df["cambio_eur_num"] = pd.to_numeric(df["cambio_eur"], errors="coerce")
    df = df.sort_values("cambio_eur_num", ascending=False)
    for _, r in df.iterrows():
        cambio = r["cambio_eur_num"]
        es_subida = r["tipo"] == "Subidas"
        punto = "🟢" if es_subida else "🔴"
        flecha = "↑" if es_subida else "↓"
        signo = "+" if es_subida else ""
        cambio_txt = f"{signo}{int(cambio):,} €".replace(",", ".") if pd.notna(cambio) else "?"
        lines.append(f"{punto} {flecha} <b>{r['jugador']}</b> ({r['equipo']}): {cambio_txt} ({r['cambio_pct']}%) — precio: {r['precio_eur']} €")
    if len(df) == 0:
        lines.append("(No se encontró ningún jugador de mis_jugadores.txt en el mercado de hoy)")
    return "\n".join(lines)


def limpia_prefijo_duplicado(nombre: str) -> str:
    m = re.match(r"^([A-ZÁÉÍÓÚÑ]{1,3})([A-ZÁÉÍÓÚÑ][a-záéíóúñ].*)$", nombre)
    if m:
        return m.group(2)
    return nombre


def split_jugador_cell(text: str):
    for pos in POSITIONS:
        idx = text.find(pos)
        if idx != -1:
            nombre = limpia_prefijo_duplicado(text[:idx].strip())
            equipo = text[idx + len(pos):].strip()
            return nombre, pos, equipo
    return limpia_prefijo_duplicado(text.strip()), "", ""


def parse_change_cell(text: str):
    text = text.replace("\xa0", " ")
    m_val = re.search(r"([+-]?[\d.]+)\s*€", text)
    m_pct = re.search(r"([+-]?[\d,]+)\s*%", text)
    valor = m_val.group(1).replace(".", "") if m_val else None
    pct = m_
