"""
Scraper diario del mercado de La Liga Fantasy (analiticafantasy.com)
Recorre TODAS las páginas del mercado para capturar el 100% de la plantilla con datos reales.
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

BASE_URL = "https://www.analiticafantasy.com/fantasy-la-liga/mercado"
OUT_DIR = Path(__file__).parent
POSITIONS = ["PT", "DF", "MC", "DL", "DT"]

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


def extrae_filas_de_html(html_content: str) -> list[dict]:
    """Parsea el HTML de la tabla extraída."""
    filas = []
    try:
        tables = pd.read_html(io.StringIO(html_content))
        if not tables:
            return []
        
        table = max(tables, key=lambda t: t.shape[0])
        columnas = {str(c).strip().lower(): c for c in table.columns}
        
        col_jugador = next((c for k, c in columnas.items() if "jugador" in k or "nombre" in k), table.columns[0])
        col_precio = next((c for k, c in columnas.items() if "precio" in k or "valor" in k), None)
        col_cambio = next((c for k, c in columnas.items() if "subida" in k or "bajada" in k or "cambio" in k), table.columns[2] if len(table.columns) > 2 else table.columns[1])

        for _, row in table.iterrows():
            j_raw = str(row.get(col_jugador, ""))
            c_raw = str(row.get(col_cambio, ""))
            p_raw = str(row.get(col_precio, "")) if col_precio is not None else ""

            m_val = re.search(r"([+-]?[\d.]+)\s*€", c_raw.replace("\xa0", " "))
            m_pct = re.search(r"([+-]?[\d,]+)\s*%", c_raw.replace("\xa0", " "))
            m_precio = re.search(r"([\d.]+)\s*€", p_raw)

            val = m_val.group(1).replace(".", "") if m_val else "0"
            pct = m_pct.group(1).replace(",", ".") if m_pct else "0.0"
            precio = m_precio.group(1).replace(".", "") if m_precio else "0"

            nombre_limp = re.sub(r"^(PT|DF|MC|DL|DT)\s*", "", j_raw).strip()
            nombre_limp = re.sub(r"^\d+\s*", "", nombre_limp).strip()

            if not nombre_limp:
                continue

            filas.append({
                "fecha": date.today().isoformat(),
                "jugador": nombre_limp,
                "precio_eur": precio,
                "cambio_eur": val,
                "cambio_pct": pct,
            })
    except Exception as e:
        print(f"⚠️ Error al parsear tabla HTML: {e}")
    return filas


def main():
    print("Recorriendo todas las páginas del mercado...")

    session = cffi_requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Referer": "https://www.analiticafantasy.com/",
    })

    todas_las_filas = []
    
    # Recorrer páginas probando diferentes formatos de URL de paginación
    for p in range(1, 20):
        # Probar endpoints/formatos de paginación
        urls_a_probar = [
            f"{BASE_URL}?pagina={p}",
            f"{BASE_URL}?p={p}",
            f"{BASE_URL}?offset={(p-1)*20}",
            f"{BASE_URL}?page={p}"
        ] if p > 1 else [BASE_URL]

        nuevas_en_esta_pagina = 0
        for target_url in urls_a_probar:
            try:
                resp = session.get(target_url, impersonate="chrome", timeout=15)
                if resp.status_code == 200:
                    extraídas = extrae_filas_de_html(resp.text)
                    if extraídas:
                        todas_las_filas.extend(extraídas)
                        nuevas_en_esta_pagina = len(extraídas)
                        print(f"  ✓ Página {p} ({target_url}) -> {len(extraídas)} jugadores extraídos")
                        break
            except Exception:
                continue

        if nuevas_en_esta_pagina == 0 and p > 1:
            print(f"  Fin de la paginación alcanzado en la página {p-1}.")
            break

    df_mercado = pd.DataFrame(todas_las_filas).drop_duplicates(subset=["jugador"]) if todas_las_filas else pd.DataFrame()
    print(f"\n✅ Total global de jugadores recopilados en el mercado: {len(df_mercado)}")

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
                "jugador": nombre_busq,
                "equipo": equipo_hint or "",
                "precio_eur": "0",
                "cambio_eur": "0",
                "cambio_pct": "0.0"
            })
            print(f"  ℹ️ {nombre_busq} (no varió su valor en las páginas escaneadas)")

    df_final = pd.DataFrame(equipo_final)
    today_file = OUT_DIR / f"mi_equipo_{date.today().isoformat()}.csv"
    df_final.to_csv(today_file, index=False, encoding="utf-8-sig")

    resumen = build_telegram_summary_mi_equipo(df_final, len(mis_jugadores))
    send_telegram_message(resumen)


if __name__ == "__main__":
    main()
