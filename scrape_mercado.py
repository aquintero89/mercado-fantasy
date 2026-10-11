"""
Scraper diario del mercado de La Liga Fantasy (analiticafantasy.com)
Versión completa con paginación automática para recorrer todas las páginas.
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
    Sistema de coincidencia estricto para evitar falsos positivos.
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

    if len(palabras_lista) >= 2:
        if all(any(pl == pm or (len(pl) >= 3 and pl in pm) for pm in palabras_mercado) for pl in palabras_lista):
            return 4
        return None

    palabra_unica = palabras_lista[0]
    if len(palabra_unica) >= 3:
        if any(palabra_unica == pm or (len(palabra_unica) >= 4 and palabra_unica in pm) for pm in palabras_mercado):
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

        indices_elegidos.append(mejores[0][1])
        print(f"  ✓  '{linea}' → {mejores[0][2]} ({mejores[0][3]})  [score {mejor_score}]")

    if no_encontrados:
        print(f"\n  ❌ No encontrados en el mercado hoy ({len(no_encontrados)}):")
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
        lines.append("(No se encontró ningún jugador de mis_jugadores.txt en el mercado)")
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

            lines.append(
                f"{punto} {flecha} <b>{r['jugador']}</b> ({r['equipo']}): "
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
    print("Consultando el mercado con paginación automática...")

    session = cffi_requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Referer": "https://www.analiticafantasy.com/",
    })

    page = 1
    max_pages = 15  safety limit

    while page <= max_pages:
        url = f"{BASE_URL}?page={page}" if page > 1 else BASE_URL
        print(f"Descargando página {page}: {url}")
        
        try:
            resp = session.get(url, impersonate="chrome", timeout=30)
            if resp.status_code != 200:
                print(f"  -> Fin de páginas o error HTTP {resp.status_code}")
                break

            html = resp.text
            tables = pd.read_html(io.StringIO(html))
            if not tables:
                print("  -> No se encontraron tablas en esta página.")
                break

            table = max(tables, key=lambda t: t.shape[0])
            if table.empty or len(table) < 2:
                print("  -> Tabla vacía o sin registros suficientes.")
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

                if not nombre or not posicion:
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

            # Si la página no aportó nuevos registros, terminamos
            if len(rows) == filas_antes:
                print("  -> No hay más registros nuevos. Fin de la paginación.")
                break

            page += 1

        except Exception as e:
            print(f"❌ Error en página {page}: {e}")
            break

    if not rows:
        print("❌ No se extrajeron datos en ninguna página.")
        sys.exit(1)

    df = pd.DataFrame(rows).drop_duplicates(subset=["fecha", "jugador", "equipo"])
    print(f"\n✅ Total de jugadores únicos capturados en total: {len(df)}")

    mis_jugadores = carga_mis_jugadores()
    if mis_jugadores:
        print("\nFiltrando jugadores de tu equipo...")
        mi_equipo_df = filtra_mi_equipo(df, mis_jugadores)
        resumen = build_telegram_summary_mi_equipo(mi_equipo_df, len(mi_equipo_df), len(mis_jugadores))
        send_telegram_message(resumen)


if __name__ == "__main__":
    main()
