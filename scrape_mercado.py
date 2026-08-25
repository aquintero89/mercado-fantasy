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

# --- Configuración de Telegram (opcional) ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TOP_N = 10  # cuántos jugadores mostrar por categoría en el mensaje general

MIS_JUGADORES_FILE = OUT_DIR / "mis_jugadores.txt"


def normaliza(texto: str) -> str:
    """Quita tildes/mayúsculas para comparar nombres de forma flexible."""
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return texto.lower().strip()


def carga_mis_jugadores() -> list:
    """Carga los jugadores desde el archivo y soporta la sintaxis 'Jugador, Equipo'."""
    if not MIS_JUGADORES_FILE.exists():
        return []
    jugadores = []
    with open(MIS_JUGADORES_FILE, encoding="utf-8") as f:
        for line in f:
            linea = line.strip()
            if not linea:
                continue
            if "," in linea:
                partes = linea.split(",", 1)
                jugadores.append((partes[0].strip(), partes[1].strip()))
            else:
                jugadores.append((linea, ""))
    return jugadores


def coincide_jugador(nombre_target: str, equipo_target: str, nombre_df: str, equipo_df: str) -> bool:
    """Verifica la coincidencia estricta de nombre (por tokens completos) y opcionalmente de equipo."""
    n_target_norm = normaliza(nombre_target)
    n_df_norm = normaliza(nombre_df)

    tokens_target = n_target_norm.split()
    tokens_df = n_df_norm.split()

    if not tokens_target or not tokens_df:
        return False

    # 1. Si en mis_jugadores.txt pones una sola palabra (ej. "Rodri") sin equipo,
    # exigimos coincidencia exacta del nombre para evitar falsos positivos con otros "Rodri".
    if len(tokens_target) == 1 and not equipo_target:
        match_nombre = (n_target_norm == n_df_norm)
    else:
        # 2. Si es un nombre compuesto (ej. "Marcos Alonso"),
        # TODOS los tokens buscados deben estar en el nombre del mercado.
        match_nombre = all(t in tokens_df for t in tokens_target)

    if not match_nombre:
        return False

    # 3. Validar equipo si se especificó en mis_jugadores.txt
    if equipo_target:
        eq_target_norm = normaliza(equipo_target)
        eq_df_norm = normaliza(equipo_df)
        if eq_target_norm not in eq_df_norm and eq_df_norm not in eq_target_norm:
            return False

    return True


def filtra_mi_equipo(df: pd.DataFrame, mis_jugadores: list) -> pd.DataFrame:
    """Filtra el DataFrame haciendo coincidir nombre y equipo según mis_jugadores.txt."""
    if not mis_jugadores or df.empty:
        return df.iloc[0:0]

    indices = []
    for idx, row in df.iterrows():
        j_df = str(row["jugador"])
        e_df = str(row["equipo"])

        for n_target, e_target in mis_jugadores:
            if coincide_jugador(n_target, e_target, j_df, e_df):
                indices.append(idx)
                break

    return df.loc[indices]


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


def scrape_tab(page, tab_label: str, rows: list):
    try:
        page.get_by_text(tab_label, exact=True).click()
        page.wait_for_timeout(1500)
    except Exception:
        print(f"  Aviso: no encontré el botón '{tab_label}', sigo con la vista actual.")

    page_num = 1
    seen_first_row = None
    paginas_sin_avanzar = 0

    while True:
        page.wait_for_timeout(1000)
        table = leer_tabla_actual(page)

        if table is None or table.empty:
            break

        first_row_sig = tuple(table.iloc[0].astype(str))

        if first_row_sig == seen_first_row:
            paginas_sin_avanzar += 1
            if paginas_sin_avanzar <= 3:
                page.wait_for_timeout(1500)
                table = leer_tabla_actual(page)
                if table is None or table.empty:
                    break
                first_row_sig = tuple(table.iloc[0].astype(str))
                if first_row_sig == seen_first_row:
                    continue
            else:
                break
        else:
            paginas_sin_avanzar = 0

        seen_first_row = first_row_sig

        for _, row in table.iterrows():
            jugador_raw = str(row.get("Jugador", ""))
            subida_raw = str(row.get("Subida", row.get("Bajada", "")))
            precio_raw = str(row.get("Precio", ""))

            nombre, posicion, equipo = split_jugador_cell(jugador_raw)
            valor, pct = parse_change_cell(subida_raw)
            precio = parse_precio_cell(precio_raw)

            if not nombre or not posicion:
                continue

            valor_num = float(valor) if valor not in (None, "") else 0.0
            tipo_real = "Subidas" if valor_num >= 0 else "Bajadas"

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

        print(f"  {tab_label} - página {page_num}: {len(table)} filas (total acumulado: {len(rows)})")

        siguiente = page.get_by_text("Siguiente", exact=True)
        if siguiente.count() == 0 or not siguiente.first.is_enabled():
            break
        siguiente.first.click()
        page_num += 1
        if page_num > 45:
            print("  Límite de seguridad de páginas alcanzado.")
            break


def main():
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        print(f"Abriendo {URL} ...")
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        print("Extrayendo mercado completo...")
        scrape_tab(page, "Subidas", rows)

        browser.close()

    if not rows:
        print("No se extrajeron datos. La estructura de la web pudo haber cambiado.")
        sys.exit(1)

    df = pd.DataFrame(rows).drop_duplicates(subset=["fecha", "jugador", "equipo"])
    print(f"\nTotal de jugadores únicos capturados hoy: {len(df)}")

    today_file = OUT_DIR / f"mercado_{date.today().isoformat()}.csv"
    df.to_csv(today_file, index=False, encoding="utf-8-sig")
    print(f"Guardado: {today_file} ({len(df)} filas)")

    historico_file = OUT_DIR / "historico.csv"
    if historico_file.exists():
        hist = pd.read_csv(historico_file, dtype=str)
        hist = hist[hist["fecha"] != date.today().isoformat()]
        combinado = pd.concat([hist, df], ignore_index=True)
    else:
        combinado = df
    combinado.to_csv(historico_file, index=False, encoding="utf-8-sig")
    print(f"Histórico actualizado: {historico_file} ({len(combinado)} filas totales)")

    mis_jugadores = carga_mis_jugadores()
    if mis_jugadores:
        mi_equipo_df = filtra_mi_equipo(df, mis_jugadores)
        mi_equipo_file = OUT_DIR / f"mi_equipo_{date.today().isoformat()}.csv"
        mi_equipo_df.to_csv(mi_equipo_file, index=False, encoding="utf-8-sig")
        print(f"Mi equipo ({len(mi_equipo_df)}/{len(mis_jugadores)} encontrados): {mi_equipo_file}")

        no_encontrados = []
        for n_target, e_target in mis_jugadores:
            encontrado = any(
                coincide_jugador(n_target, e_target, row["jugador"], row["equipo"])
                for _, row in mi_equipo_df.iterrows()
            )
            if not encontrado:
                txt = f"{n_target} ({e_target})" if e_target else n_target
                no_encontrados.append(txt)

        if no_encontrados:
            print(f"  No encontrados en el mercado de hoy: {', '.join(no_encontrados)}")

        historico_equipo_file = OUT_DIR / "historico_mi_equipo.csv"
        if historico_equipo_file.exists():
            hist_eq = pd.read_csv(historico_equipo_file, dtype=str)
            hist_eq = hist_eq[hist_eq["fecha"] != date.today().isoformat()]
            combinado_eq = pd.concat([hist_eq, mi_equipo_df], ignore_index=True)
        else:
            combinado_eq = mi_equipo_df
        combinado_eq.to_csv(historico_equipo_file, index=False, encoding="utf-8-sig")
        print(f"Histórico de mi equipo actualizado: {historico_equipo_file}")

        resumen = build_telegram_summary_mi_equipo(mi_equipo_df, len(mi_equipo_df), len(mis_jugadores))
    else:
        print("No hay 'mis_jugadores.txt' (o está vacío) — se envía el resumen general del mercado.")
        resumen = build_telegram_summary(df)

    send_telegram_message(resumen)


if __name__ == "__main__":
    main()
