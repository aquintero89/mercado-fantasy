"""
Scraper diario del mercado de La Liga Fantasy (analiticafantasy.com)
=====================================================================
Versión completa — recorre TODAS las páginas del mercado.

Mejoras anti-bloqueo:
- User-Agent real + viewport + locale
- Oculta navigator.webdriver
- Reintentos con detección de página 403
- Esperas más robustas

Instalación (una sola vez):
    pip install playwright pandas requests lxml html5lib beautifulsoup4 --break-system-packages
    playwright install chromium

Uso:
    python3 scrape_mercado_corregido.py
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
POSITIONS = ["PT", "DF", "MC", "DL", "DT"]

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TOP_N = 10

MIS_JUGADORES_FILE = OUT_DIR / "mis_jugadores.txt"


def normaliza(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return texto.lower().strip()


def parse_entrada_jugador(linea: str):
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", linea.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return linea.strip(), None


def puntua_coincidencia(nombre_lista: str, nombre_mercado: str):
    palabras_lista = set(normaliza(nombre_lista).split())
    palabras_mercado = set(normaliza(nombre_mercado).split())
    if not palabras_lista or not palabras_mercado:
        return None
    if palabras_lista == palabras_mercado:
        return 3
    if palabras_lista <= palabras_mercado:
        return 2
    if palabras_mercado <= palabras_lista:
        return 1
    return None


def coincide_por_palabras(nombre_lista: str, nombre_mercado: str) -> bool:
    return puntua_coincidencia(nombre_lista, nombre_mercado) is not None


def carga_mis_jugadores() -> list:
    if not MIS_JUGADORES_FILE.exists():
        return []
    with open(MIS_JUGADORES_FILE, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def filtra_mi_equipo(df: pd.DataFrame, entradas: list) -> pd.DataFrame:
    if not entradas:
        return df.iloc[0:0]

    indices_elegidos = []
    ambiguos = []

    for linea in entradas:
        nombre, equipo_hint = parse_entrada_jugador(linea)
        candidatos = []

        for idx, row in df.iterrows():
            if equipo_hint and normaliza(equipo_hint) not in normaliza(str(row["equipo"])):
                continue
            score = puntua_coincidencia(nombre, str(row["jugador"]))
            if score is not None:
                candidatos.append((score, idx, row["jugador"], row["equipo"]))

        if not candidatos:
            continue

        mejor_score = max(c[0] for c in candidatos)
        mejores = [c for c in candidatos if c[0] == mejor_score]

        if len(mejores) > 1:
            nombres = ", ".join(f"{c[2]} ({c[3]})" for c in mejores)
            ambiguos.append(f"{linea} -> {nombres}")

        indices_elegidos.append(mejores[0][1])

    if ambiguos:
        print("  Coincidencias ambiguas (añade el equipo entre paréntesis):")
        for a in ambiguos:
            print(f"    {a}")

    vistos = set()
    unicos = [i for i in indices_elegidos if not (i in vistos or vistos.add(i))]
    return df.loc[unicos]


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
    }, timeout=15)
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
    df = df.copy()
    df["cambio_eur_num"] = pd.to_numeric(df["cambio_eur"], errors="coerce")

    n_subidas = int((df["tipo"] == "Subidas").sum())
    n_bajadas = int((df["tipo"] == "Bajadas").sum())
    suma_subidas = df.loc[df["tipo"] == "Subidas", "cambio_eur_num"].sum()
    suma_bajadas = df.loc[df["tipo"] == "Bajadas", "cambio_eur_num"].sum()
    balance_total = df["cambio_eur_num"].sum()

    def fmt(n):
        return f"{int(n):,}".replace(",", ".")

    signo_balance = "+" if balance_total >= 0 else ""

    lines = [
        f"<b>⚽ Mi equipo — {hoy}</b> ({encontrados}/{total} encontrados)",
        "",
        f"🟢 Subidas: {n_subidas} (+{fmt(suma_subidas)} €)    🔴 Bajadas: {n_bajadas} ({fmt(suma_bajadas)} €)",
        f"💰 Balance total: {signo_balance}{fmt(balance_total)} €",
        "",
    ]

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
    nombre = re.sub(r"^\d+", "", nombre).strip()
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
    pct = m_pct.group(1).replace(",", ".") if m_pct else None
    return valor, pct


def parse_precio_cell(text: str):
    m = re.search(r"([\d.]+)\s*€", text)
    return m.group(1).replace(".", "") if m else None


def leer_tabla_actual(page):
    html = page.content()
    try:
        tables = pd.read_html(io.StringIO(html))
    except ValueError:
        return None
    if not tables:
        return None
    return max(tables, key=lambda t: t.shape[0])


def abrir_pagina_con_reintentos(page, intentos=3):
    """Abre el mercado y distingue un fallo de navegación de un bloqueo del servidor."""
    for intento in range(1, intentos + 1):
        print(f"Abriendo {URL} (intento {intento}/{intentos}) ...")
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"  Error de navegación: {e}")
            if intento < intentos:
                page.wait_for_timeout(3000 * intento)
            continue

        content = page.content().lower()
        indicadores_bloqueo = (
            "you have been blocked",
            "sorry, you have been blocked",
            "bunny.net",
            "access denied",
            "request blocked",
        )
        if any(indicador in content for indicador in indicadores_bloqueo):
            print("  La web ha devuelto una página de bloqueo (anti-bot/CDN).")
            print("  El script se detiene sin guardar datos incompletos.")
            print("  Prueba más tarde o consulta al administrador del sitio por un acceso permitido.")
            return False