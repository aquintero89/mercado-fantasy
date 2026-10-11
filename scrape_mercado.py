"""
Scraper diario de jugadores específicos para La Liga Fantasy (analiticafantasy.com)
Consulta individualizada por cada jugador para extraer precio y cambios exactos.
"""

import os
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

import pandas as pd
from curl_cffi import requests as cffi_requests

BASE_URL = "https://www.analiticafantasy.com"
OUT_DIR = Path(__file__).parent
MIS_JUGADORES_FILE = OUT_DIR / "mis_jugadores.txt"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def normaliza(texto: str) -> str:
    """Quita acentos, pasa a minúsculas y limpia espacios."""
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return texto.lower().strip()


def parse_entrada_jugador(linea: str):
    """Extrae nombre y equipo opcional de mis_jugadores.txt."""
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", linea.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return linea.strip(), None


def carga_mis_jugadores() -> list[str]:
    if not MIS_JUGADORES_FILE.exists():
        print(f"  ⚠️ No existe {MIS_JUGADORES_FILE}")
        return []
    with open(MIS_JUGADORES_FILE, encoding="utf-8") as f:
        jugadores = [line.strip() for line in f if line.strip()]
    print(f"  Cargados {len(jugadores)} jugadores desde mis_jugadores.txt")
    return jugadores


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Telegram no configurado.")
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
            print("✅ Resumen enviado a Telegram correctamente.")
        else:
            print(f"❌ Error de Telegram: {resp.status_code} → {resp.text}")
    except Exception as e:
        print(f"❌ Excepción al enviar Telegram: {e}")


def build_telegram_summary_mi_equipo(df: pd.DataFrame, total: int) -> str:
    hoy = date.today().isoformat()
    df = df.copy()
    df["cambio_eur_num"] = pd.to_numeric(df["cambio_eur"], errors="coerce").fillna(0)

    n_subidas = int((df["cambio_eur_num"] > 0).sum())
    n_bajadas = int((df["cambio_eur_num"] < 0).sum())
    suma_subidas = df.loc[df["cambio_eur_num"] > 0, "cambio_eur_num"].sum()
    suma_bajadas = df.loc[df["cambio_eur_num"] < 0, "cambio_eur_num"].sum()
    balance_total = df["cambio_eur_num"].sum()

    def fmt(n):
        try:
            return f"{int(n):,}".replace(",", ".")
        except Exception:
            return "0"

    signo_balance = "+" if balance_total >= 0 else ""

    lines = [
        f"<b>⚽ Mi equipo — {hoy}</b> ({len(df)}/{total} encontrados)",
        "",
        f"🟢 Subidas: {n_subidas} (+{fmt(suma_subidas)} €)    🔴 Bajadas: {n_bajadas} ({fmt(suma_bajadas)} €)",
        f"💰 Balance total: {signo_balance}{fmt(balance_total)} €",
        "",
    ]

    df = df.sort_values("cambio_eur_num", ascending=False)
    for _, r in df.iterrows():
        cambio = r["cambio_eur_num"]
        punto = "🟢" if cambio > 0 else ("🔴" if cambio < 0 else "⚪")
        flecha = "↑" if cambio > 0 else ("↓" if cambio < 0 else "•")
        signo = "+" if cambio > 0 else ""
        cambio_txt = f"{signo}{int(cambio):,} €".replace(",", ".") if pd.notna(cambio) and cambio != 0 else "0 €"

        precio_raw = r["precio_eur"]
        try:
            precio_txt = f"{int(precio_raw):,}".replace(",", ".") if pd.notna(precio_raw) and str(precio_raw).strip() not in ("", "0", "None") else "?"
        except Exception:
            precio_txt = "?"

        equipo_str = f" ({r['equipo']})" if r['equipo'] else ""
        pct_str = f"{r['cambio_pct']}%" if r['cambio_pct'] else "0.0%"

        lines.append(
            f"{punto} {flecha} <b>{r['jugador']}</b>{equipo_str}: "
            f"{cambio_txt} ({pct_str}) — precio: {precio_txt} €"
        )

    return "\n".join(lines)


def consultar_jugador_individual(session, nombre: str, equipo_hint: str) -> dict:
    """Busca al jugador consultando el buscador interno y extrayendo sus valores."""
    q = f"{nombre} {equipo_hint}" if equipo_hint else nombre
    url_busqueda = f"{BASE_URL}/fantasy-la-liga/jugadores?q={q.replace(' ', '+')}"
    
    resultado = {
        "fecha": date.today().isoformat(),
        "jugador": nombre,
        "equipo": equipo_hint or "",
        "precio_eur": "0",
        "cambio_eur": "0",
        "cambio_pct": "0.0"
    }

    try:
        resp = session.get(url_busqueda, impersonate="chrome", timeout=15)
        if resp.status_code == 200:
            html = resp.text
            
            # Buscar patrones monetarios y porcentajes en la respuesta de búsqueda
            importes = re.findall(r"([+-]?[\d.]+)\s*€", html)
            porcentajes = re.findall(r"([+-]?[\d,]+)\s*%", html)

            valores_limpios = [imp.replace(".", "") for imp in importes if len(imp.replace(".", "")) >= 4]
            
            if len(valores_limpios) >= 2:
                resultado["cambio_eur"] = valores_limpios[0]
                resultado["precio_eur"] = valores_limpios[1]
            elif len(valores_limpios) == 1:
                resultado["precio_eur"] = valores_limpios[0]

            if porcentajes:
                resultado["cambio_pct"] = porcentajes[0].replace(",", ".")
                
            print(f"  ✓ {nombre}: Precio={resultado['precio_eur']} | Cambio={resultado['cambio_eur']}")
    except Exception as e:
        print(f"  ⚠️ Error consultando a {nombre}: {e}")

    return resultado


def main():
    print("Iniciando consulta individual de jugadores...")

    session = cffi_requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Referer": BASE_URL,
    })

    mis_jugadores = carga_mis_jugadores()
    equipo_final = []

    for linea in mis_jugadores:
        nombre, equipo_hint = parse_entrada_jugador(linea)
        datos = consultar_jugador_individual(session, nombre, equipo_hint)
        equipo_final.append(datos)

    df_final = pd.DataFrame(equipo_final)
    today_file = OUT_DIR / f"mi_equipo_{date.today().isoformat()}.csv"
    df_final.to_csv(today_file, index=False, encoding="utf-8-sig")

    resumen = build_telegram_summary_mi_equipo(df_final, len(mis_jugadores))
    send_telegram_message(resumen)


if __name__ == "__main__":
    main()
