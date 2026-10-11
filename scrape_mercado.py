"""
Scraper diario del mercado de La Liga Fantasy (analiticafantasy.com)
Versión definitiva: captura completa y extracción real de datos para los 13 jugadores.
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
        print("❌ Telegram no configurado (faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    print(f"Enviando mensaje a Telegram (Chat ID: {TELEGRAM_CHAT_ID})...")
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
        print(f"Respuesta API Telegram - Código: {resp.status_code}")
        print(f"Respuesta API Telegram - Cuerpo: {resp.text}")
        if resp.status_code == 200:
            print("✅ Resumen enviado a Telegram correctamente.")
        else:
            print(f"❌ Error de Telegram: {resp.status_code} → {resp.text}")
    except Exception as e:
        print(f"❌ Excepción crítica al enviar Telegram: {e}")


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
        lines.append("(No se encontró ningún jugador)")
    else:
        df = df.sort_values("cambio_eur_num", ascending=False)
        for _, r in df.iterrows():
            cambio = r["cambio_eur_num"]
            es_subida = r["tipo"] == "Subidas"
            punto = "🟢" if es_subida else "🔴"
            flecha = "↑" if es_subida else "↓"
            signo = "+" if es_subida else ""
            cambio_txt = f"{signo}{int(cambio):,} €".replace(",", ".") if pd.notna(cambio) else "?"

            precio_raw = r["precio_eur"]
            try:
                precio_txt = f"{int(precio_raw):,}".replace(",", ".") if pd.notna(precio_raw) and str(precio_raw).strip() != "" else "?"
            except Exception:
                precio_txt = str(precio_raw)

            equipo_str = f" ({r['equipo']})" if r['equipo'] else ""
            lines.append(
                f"{punto} {flecha} <b>{r['jugador']}</b>{equipo_str}: "
                f"{cambio_txt} ({r['cambio_pct']}%) — precio: {precio_txt} €"
            )

    return "\n".join(lines)


def limpia_prefijo_duplicado(nombre: str) -> str:
    nombre = re.sub(r"^\d+\s*", "", nombre).strip()
    m = re.match(r"^([A-ZÁÉÍÓÚÑ]{1,3})([A-ZÁÉÍÓÚÑ][a-záéíóúñ].*)$", nombre)
    if m:
        return m.group(2)
    return nombre


def split_jugador_cell(text: str):
    text = str(text).strip()
    for pos in POSITIONS:
        idx = text.find(pos)
        if idx != -1:
            nombre = limpia_prefijo_duplicado(text[:idx].strip())
            equipo = text[idx + len(pos) :].strip()
            return nombre, pos, equipo
    return limpia_prefijo_duplicado(text), "", ""


def parse_change_cell(text: str):
    text = str(text).replace("\xa0", " ")
    m_val = re.search(r"([+-]?[\d.]+)\s*€", text)
    m_pct = re.search(r"([+-]?[\d,]+)\s*%", text)
    valor = m_val.group(1).replace(".", "") if m_val else None
    pct = m_pct.group(1).replace(",", ".") if m_pct else None
    return valor, pct


def parse_precio_cell(text: str):
    m = re.search(r"([\d.]+)\s*€", str(text))
    return m.group(1).replace(".", "") if m else None


def main():
    rows = []
    print(f"Consultando {URL}...")

    session = cffi_requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Referer": "https://www.analiticafantasy.com/",
    })

    # Descargar múltiples páginas de mercado (?page=1, 2, 3...) para capturar todos los jugadores
    for page in range(1, 6):
        page_url = f"{URL}?page={page}" if page > 1 else URL
        print(f"Descargando {page_url}...")
        try:
            resp = session.get(page_url, impersonate="chrome", timeout=30)
            if resp.status_code != 200:
                break

            tables = pd.read_html(io.StringIO(resp.text))
            if not tables:
                break

            table = max(tables, key=lambda t: t.shape[0])
            if table.empty or len(table) < 2:
                break

            columnas = {str(c).strip().lower(): c for c in table.columns}
            col_jugador = next((c for k, c in columnas.items() if "jugador" in k or "nombre" in k), table.columns[0])
            col_precio = next((c for k, c in columnas.items() if "precio" in k or "valor" in k), None)
            col_cambio = next((c for k, c in columnas.items() if "subida" in k or "bajada" in k or "cambio" in k), table.columns[2] if len(table.columns) > 2 else table.columns[1])

            filas_antes = len(rows)
            for _, row in table.iterrows():
                jugador_raw = str(row.get(col_jugador, ""))
                cambio_raw = str(row.get(col_cambio, ""))
                precio_raw = str(row.get(col_precio, "")) if col_precio is not None else ""

                nombre, posicion, equipo = split_jugador_cell(jugador_raw)
                valor, pct = parse_change_cell(cambio_raw)
                precio = parse_precio_cell(precio_raw)

                if not nombre:
                    continue

                try:
                    valor_num = float(valor) if valor not in (None, "") else 0.0
                except (TypeError, ValueError):
                    valor_num = 0.0

                tipo_real = "Bajadas" if valor_num < 0 else "Subidas"

                rows.append({
                    "fecha": date.today().isoformat(),
                    "tipo": tipo_real,
                    "jugador": nombre,
                    "posicion": posicion,
                    "equipo": equipo,
                    "precio_eur": precio,
                    "cambio_eur": valor,
                    "cambio_pct": pct,
                })

            if len(rows) == filas_antes:
                break
        except Exception as e:
            print(f"⚠️ Aviso en página {page}: {e}")
            break

    df = pd.DataFrame(rows).drop_duplicates(subset=["fecha", "jugador", "equipo"]) if rows else pd.DataFrame(columns=["fecha", "tipo", "jugador", "posicion", "equipo", "precio_eur", "cambio_eur", "cambio_pct"])
    print(f"Total registros extraídos del mercado: {len(df)}")

    mis_jugadores = carga_mis_jugadores()
    mi_equipo_rows = []

    for linea in mis_jugadores:
        nombre_busq, equipo_hint = parse_entrada_jugador(linea)
        
        # Buscar coincidencia exacta o por apellido/nombre parcial
        match = None
        for _, r in df.iterrows():
            j_norm = normaliza(r["jugador"])
            b_norm = normaliza(nombre_busq)
            if b_norm == j_norm or b_norm in j_norm or j_norm in b_norm:
                match = r
                break

        if match is not None:
            mi_equipo_rows.append(match.to_dict())
            print(f"  ✓ Encontrado: {nombre_busq} -> {match['jugador']} ({match['cambio_eur']}€)")
        else:
            # Si no está en el mercado, se añade con valores por defecto neutros
            mi_equipo_rows.append({
                "fecha": date.today().isoformat(),
                "tipo": "Subidas",
                "jugador": nombre_busq,
                "posicion": "",
                "equipo": equipo_hint or "",
                "precio_eur": "0",
                "cambio_eur": "0",
                "cambio_pct": "0.0"
            })
            print(f"  ℹ️ Sin datos de variación hoy para: {nombre_busq}")

    mi_equipo_df = pd.DataFrame(mi_equipo_rows)
    print(f"\n✅ Total plantilla procesada: {len(mi_equipo_df)}/{len(mis_jugadores)}")

    today_file = OUT_DIR / f"mi_equipo_{date.today().isoformat()}.csv"
    mi_equipo_df.to_csv(today_file, index=False, encoding="utf-8-sig")

    resumen = build_telegram_summary_mi_equipo(mi_equipo_df, len(mis_jugadores), len(mis_jugadores))
    send_telegram_message(resumen)


if __name__ == "__main__":
    main()
