"""
Scraper diario del mercado de La Liga Fantasy (analiticafantasy.com)
Utiliza Playwright para renderizar la tabla dinámica mediante JavaScript.
"""

import os
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright
from curl_cffi import requests as cffi_requests

URL = "https://www.analiticafantasy.com/fantasy-la-liga/mercado"
OUT_DIR = Path(__file__).parent

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
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


def carga_mis_jugadores() -> list[str]:
    if not MIS_JUGADORES_FILE.exists():
        print(f"  ⚠️  No existe {MIS_JUGADORES_FILE}")
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


def main():
    rows = []
    print("Iniciando navegador Playwright para renderizar la tabla dinámicamente...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        print(f"Cargando URL: {URL}")
        page.goto(URL, wait_until="networkidle", timeout=60000)
        
        # Esperar a que las filas de la tabla estén presentes en el DOM
        try:
            page.wait_for_selector("table tbody tr", timeout=15000)
        except Exception:
            print("⚠️ Timeout esperando selectores de la tabla.")

        tr_elements = page.query_selector_all("table tbody tr")
        print(f"Filas detectadas en la página: {len(tr_elements)}")

        for tr in tr_elements:
            texto = tr.inner_text().strip()
            if not texto:
                continue

            partes = [p.strip() for p in texto.split("\n") if p.strip()]
            if len(partes) < 3:
                continue

            # Extraer jugador, cambio y precio usando expresiones regulares sobre el texto de la fila
            # Ejemplo texto fila: "1\nMbappé\nDL Real Madrid\n+2.810.254 €\n+1,9%\n148.663.415 €"
            raw_text = " ".join(partes)
            
            # Subida/Bajada (ejemplo: +2.810.254 € o -150.000 €)
            m_cambio = re.search(r"([+-]?[\d.]+)\s*€\s*([+-]?[\d,]+)%", raw_text)
            # Precio (último importe en euros)
            m_precios = re.findall(r"([\d.]+)\s*€", raw_text)

            if m_precios:
                precio_val = m_precios[-1].replace(".", "")
            else:
                precio_val = "0"

            if m_cambio:
                cambio_val = m_cambio.group(1).replace(".", "")
                pct_val = m_cambio.group(2).replace(",", ".")
            else:
                cambio_val = "0"
                pct_val = "0.0"

            # Extraer el nombre del jugador limpiando número de posición inicial y club
            nombre_jugador = partes[1] if len(partes) > 1 else partes[0]
            if nombre_jugador.isdigit() and len(partes) > 2:
                nombre_jugador = partes[2]

            # Limpiar nombre
            nombre_jugador = re.sub(r"^\d+\s*", "", nombre_jugador).strip()

            try:
                val_num = float(cambio_val)
            except ValueError:
                val_num = 0.0

            tipo_real = "Subidas" if val_num >= 0 else "Bajadas"

            rows.append({
                "fecha": date.today().isoformat(),
                "tipo": tipo_real,
                "jugador": nombre_jugador,
                "precio_eur": precio_val,
                "cambio_eur": cambio_val,
                "cambio_pct": pct_val,
            })

        browser.close()

    df_mercado = pd.DataFrame(rows).drop_duplicates(subset=["jugador"]) if rows else pd.DataFrame()
    print(f"Registros de mercado extraídos correctamente: {len(df_mercado)}")

    mis_jugadores = carga_mis_jugadores()
    equipo_final = []

    for linea in mis_jugadores:
        nombre_busq, equipo_hint = parse_entrada_jugador(linea)
        match = None

        if not df_mercado.empty:
            for _, r in df_mercado.iterrows():
                j_norm = normaliza(r["jugador"])
                b_norm = normaliza(nombre_busq)
                if b_norm == j_norm or b_norm in j_norm or j_norm in b_norm:
                    match = r
                    break

        if match is not None:
            dic = match.to_dict()
            dic["equipo"] = equipo_hint or ""
            equipo_final.append(dic)
            print(f"  ✓ {nombre_busq} -> {match['jugador']} | Cambio: {match['cambio_eur']} € | Precio: {match['precio_eur']} €")
        else:
            equipo_final.append({
                "fecha": date.today().isoformat(),
                "tipo": "Subidas",
                "jugador": nombre_busq,
                "equipo": equipo_hint or "",
                "precio_eur": "0",
                "cambio_eur": "0",
                "cambio_pct": "0.0"
            })
            print(f"  ℹ️ {nombre_busq} (sin variación en el bloque capturado)")

    df_final = pd.DataFrame(equipo_final)
    today_file = OUT_DIR / f"mi_equipo_{date.today().isoformat()}.csv"
    df_final.to_csv(today_file, index=False, encoding="utf-8-sig")

    resumen = build_telegram_summary_mi_equipo(df_final, len(mis_jugadores))
    send_telegram_message(resumen)


if __name__ == "__main__":
    main()
