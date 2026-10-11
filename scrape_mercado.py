"""
Scraper diario del mercado de La Liga Fantasy (analiticafantasy.com)
Versión 100% fiable con curl_cffi y cabeceras extendidas para GitHub Actions.
"""

import io
import os
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

import pandas as pd
from curl_cffi import requests as cffi_requests

URL = "https://www.analiticafantasy.com/fantasy-la-liga/mercado"
OUT_DIR = Path(__file__).parent
POSITIONS = ["PT", "DF", "MC", "DL", "DT"]

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TOP_N = 10

MIS_JUGADORES_FILE = OUT_DIR / "mis_jugadores.txt"


def normaliza(texto: str) -> str:
    """Quita acentos, pasa a minúsculas y limpia espacios."""
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return texto.lower().strip()


def parse_entrada_jugador(linea: str):
    """Extrae nombre y (opcional) equipo de una línea de mis_jugadores.txt."""
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", linea.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return linea.strip(), None


def puntua_coincidencia(nombre_lista: str, nombre_mercado: str) -> int | None:
    """
    Sistema de puntuación flexible:
      5 = coincidencia exacta (después de normalizar)
      4 = todas las palabras de la lista están en el nombre del mercado
      3 = el apellido principal coincide + alguna otra palabra
      2 = solo el apellido principal coincide
      1 = coincidencia parcial fuerte
      None = no hay coincidencia suficiente
    """
    nl = normaliza(nombre_lista)
    nm = normaliza(nombre_mercado)

    if not nl or not nm:
        return None

    if nl == nm:
        return 5

    palabras_lista = [p for p in nl.split() if p]
    palabras_mercado = [p for p in nm.split() if p]

    if not palabras_lista or not palabras_mercado:
        return None

    if all(any(pl in pm or pm in pl for pm in palabras_mercado) for pl in palabras_lista):
        return 4

    apellido = palabras_lista[-1]
    if len(apellido) < 3:
        apellido = max(palabras_lista, key=len)

    apellido_en_mercado = any(
        apellido == pm or (len(apellido) >= 4 and (apellido in pm or pm in apellido))
        for pm in palabras_mercado
    )

    if not apellido_en_mercado:
        return None

    matches = 0
    for pl in palabras_lista:
        for pm in palabras_mercado:
            if len(pl) <= 2 and pl.endswith("."):
                if pm.startswith(pl[0]):
                    matches += 1
                    break
            elif pl == pm or (len(pl) >= 3 and (pl in pm or pm in pl)):
                matches += 1
                break

    if matches == len(palabras_lista):
        return 3
    if matches >= 1 and apellido_en_mercado:
        if matches == 1:
            return 2
        return 3

    return None


def carga_mis_jugadores() -> list[str]:
    if not MIS_JUGADORES_FILE.exists():
        print(f"  ⚠️  No existe {MIS_JUGADORES_FILE}")
        return []
    with open(MIS_JUGADORES_FILE, encoding="utf-8") as f:
        jugadores = [line.strip() for line in f if line.strip()]
    print(f"  Cargados {len(jugadores)} jugadores desde mis_jugadores.txt")
    return jugadores


def filtra_mi_equipo(df: pd.DataFrame, entradas: list[str]) -> pd.DataFrame:
    if not entradas:
        return df.iloc[0:0]

    indices_elegidos = []
    no_encontrados = []
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
            no_encontrados.append(linea)
            continue

        mejor_score = max(c[0] for c in candidatos)
        mejores = [c for c in candidatos if c[0] == mejor_score]

        if len(mejores) > 1:
            nombres = ", ".join(f"{c[2]} ({c[3]})" for c in mejores)
            ambiguos.append(f"  ⚠️  '{linea}' → varias opciones: {nombres}")

        indices_elegidos.append(mejores[0][1])
        print(f"  ✓  '{linea}' → {mejores[0][2]} ({mejores[0][3]})  [score {mejor_score}]")

    if ambiguos:
        print("\n  Coincidencias ambiguas:")
        for a in ambiguos:
            print(a)

    if no_encontrados:
        print(f"\n  ❌ No encontrados ({len(no_encontrados)}):")
        for n in no_encontrados:
            print(f"     - {n}")

    vistos = set()
    unicos = []
    for i in indices_elegidos:
        if i not in vistos:
            vistos.add(i)
            unicos.append(i)

    return df.loc[unicos]


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram no configurado (faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID). Omitido.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = cffi_requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if resp.status_code == 200:
            print("✅ Resumen enviado a Telegram.")
        else:
            print(f"❌ Error enviando a Telegram: {resp.status_code} → {resp.text}")
    except Exception as e:
        print(f"❌ Excepción al enviar Telegram: {e}")


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
            lines.append(
                f"↑ {r['jugador']} ({r['equipo']}): +{cambio:,} € ({r['cambio_pct']}%)".replace(",", ".")
            )
        lines.append("")

    bajadas = df[df["tipo"] == "Bajadas"].copy()
    if not bajadas.empty:
        bajadas["cambio_eur_num"] = pd.to_numeric(bajadas["cambio_eur"], errors="coerce")
        bajadas = bajadas.sort_values("cambio_eur_num", ascending=True).head(TOP_N)
        lines.append("<b>🔴 Top bajadas</b>")
        for _, r in bajadas.iterrows():
            cambio = int(r["cambio_eur_num"]) if pd.notna(r["cambio_eur_num"]) else "?"
            lines.append(
                f"↓ {r['jugador']} ({r['equipo']}): {cambio:,} € ({r['cambio_pct']}%)".replace(",", ".")
            )

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
        try:
            return f"{int(n):,}".replace(",", ".")
        except Exception:
            return "?"

    signo_balance = "+" if balance_total >= 0 else ""

    lines = [
        f"<b>⚽ Mi equipo — {hoy}</b> ({encontrados}/{total} encontrados)",
        "",
        f"🟢 Subidas: {n_subidas} (+{fmt(suma_subidas)} €)    🔴 Bajadas: {n_bajadas} ({fmt(suma_bajadas)} €)",
        f"💰 Balance total: {signo_balance}{fmt(balance_total)} €",
        "",
    ]

    if len(df) == 0:
        lines.append("(No se encontró ningún jugador de mis_jugadores.txt en el mercado de hoy)")
