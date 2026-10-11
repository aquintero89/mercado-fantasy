"""
Scraper diario del mercado de La Liga Fantasy (analiticafantasy.com)
=====================================================================
Versión completa y robusta con camuflaje anti-bot mejorado.
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
    palabras_lista = [normaliza(p) for p in nombre_lista.split() if p.strip()]
    palabras_mercado = [normaliza(p) for p in nombre_mercado.split() if p.strip()]
    
    if not palabras_lista or not palabras_mercado:
        return None

    match_count = 0
    for pl in palabras_lista:
        if (len(pl) <= 2 and pl.endswith('.')) or len(pl) == 1:
            letra = pl[0]
            if any(pm.startswith(letra) for pm in palabras_mercado):
                match_count += 1
        elif pl in palabras_mercado:
            match_count += 1

    if match_count == len(palabras_lista):
        if palabras_lista == palabras_mercado:
            return 3
        return 2
    elif match_count > 0 and match_count >= len(palabras_lista) - 1:
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
    for intento in range(1, intentos + 1):
        print(f"Abriendo {URL} (intento {intento}/{intentos}) ...")
        try:
            page.wait_for_timeout(1500 * intento)
            page.goto(URL, wait_until="networkidle", timeout=60000)
        except Exception as e:
            print(f"  Error de navegación: {e}")
            if intento < intentos:
                page.wait_for_timeout(4000 * intento)
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
            return False

        try:
            page.wait_for_selector("table", timeout=20000)
            page.wait_for_timeout(1500)
            return True
        except Exception:
            print(f"  No apareció ninguna tabla en el intento {intento}.")
            if intento < intentos:
                page.wait_for_timeout(2000 * intento)
    return False


def cerrar_aviso_privacidad_si_aparece(page):
    opciones = [
        "Rechazar todas", "Rechazar todo", "Solo necesarias",
        "Aceptar todas", "Aceptar todo", "Aceptar",
        "Reject all", "Reject All", "Accept all", "Accept All",
        "Save settings", "Guardar configuración",
    ]
    for texto in opciones:
        try:
            boton = page.get_by_role("button", name=re.compile(re.escape(texto), re.I))
            if boton.count() and boton.first.is_visible():
                boton.first.click(timeout=1800)
                page.wait_for_timeout(700)
                return
        except Exception:
            continue


def seleccionar_pestana(page, tab_label: str) -> bool:
    cerrar_aviso_privacidad_si_aparece(page)
    try:
        objetivo = page.get_by_text(tab_label, exact=True).first
        if not objetivo.count():
            print(f"  Aviso: no aparece la pestaña '{tab_label}'.")
            return False
        objetivo.scroll_into_view_if_needed(timeout=3000)
        objetivo.click(timeout=5000)
        page.wait_for_timeout(1000)
        return True
    except Exception as e:
        print(f"  No se pudo seleccionar '{tab_label}': {e}")
        return False


def scrape_tab(page, tab_label: str, rows: list):
    if not seleccionar_pestana(page, tab_label):
        return

    page_num = 1
    firmas_vistas = set()

    while page_num <= 70:
        page.wait_for_timeout(700)
        table = leer_tabla_actual(page)
        if table is None or table.empty:
            print(f"  No hay tabla de datos en '{tab_label}'.")
            break

        firma = tuple(table.astype(str).fillna("").to_numpy().ravel())
        if firma in firmas_vistas:
            print("  La tabla no ha cambiado; se detiene la paginación para evitar un bucle.")
            break
        firmas_vistas.add(firma)

        columnas = {str(c).strip().lower(): c for c in table.columns}
        
        col_jugador = next((c for k, c in columnas.items() if "jugador" in k or "nombre" in k), None)
        col_precio = next((c for k, c in columnas.items() if "precio" in k or "valor" in k), None)
        col_cambio = next((c for k, c in columnas.items() if "subida" in k or "bajada" in k or "cambio" in k), None)

        if col_jugador is None or col_cambio is None:
            if len(table.columns) >= 3:
                col_jugador = table.columns[0]
                col_precio = table.columns[1] if len(table.columns) > 3 else None
                col_cambio = table.columns[2] if len(table.columns) > 2 else table.columns[1]
            else:
                print(f"  No reconozco las columnas de la tabla: {list(table.columns)}")
                break

        tipo_pestana = "Bajadas" if "bajada" in str(col_cambio).lower() or tab_label.lower() == "bajadas" else "Subidas"

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
            tipo_real = tipo_pestana
            if tipo_real == "Bajadas" and valor_num > 0:
                valor = str(-int(valor_num))

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
        if siguiente.count() == 0:
            break
        try:
            if not siguiente.first.is_enabled():
                break
            siguiente.first.click()
            page.wait_for_timeout(900)
        except Exception:
            break
        page_num += 1

    if page_num > 70:
        print("  Límite de seguridad de 70 páginas alcanzado.")


def limpia_csv_antiguos():
    hoy = date.today().isoformat()
    for patron in ["mercado_*.csv", "mi_equipo_*.csv"]:
        for f in OUT_DIR.glob(patron):
            if hoy not in f.name:
                f.unlink()
                print(f"  Borrado archivo antiguo: {f.name}")


def main():
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="es-ES",
            timezone_id="Europe/Madrid",
            java_script_enabled=True,
        )
        # Ocultar rastro de automatización estrictamente
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        page = context.new_page()

        if not abrir_pagina_con_reintentos(page):
            print("No se pudo cargar la tabla tras varios intentos.")
            browser.close()
            sys.exit(1)

        print("Extrayendo mercado...")
        scrape_tab(page, "Subidas", rows)
        scrape_tab(page, "Bajadas", rows)

        browser.close()

    if not rows:
        print("No se extrajeron datos.")
        sys.exit(1)

    df = pd.DataFrame(rows).drop_duplicates(subset=["fecha", "jugador", "equipo"])
    print(f"\nTotal de jugadores únicos capturados hoy: {len(df)}")

    today_file = OUT_DIR / f"mercado_{date.today().isoformat()}.csv"
    df.to_csv(today_file, index=False, encoding="utf-8-sig")

    historico_file = OUT_DIR / "historico.csv"
    if historico_file.exists():
        hist = pd.read_csv(historico_file, dtype=str)
        hist = hist[hist["fecha"] != date.today().isoformat()]
        combinado = pd.concat([hist, df], ignore_index=True)
    else:
        combinado = df
    combinado.to_csv(historico_file, index=False, encoding="utf-8-sig")

    mis_jugadores = carga_mis_jugadores()
    if mis_jugadores:
        mi_equipo_df = filtra_mi_equipo(df, mis_jugadores)
        mi_equipo_file = OUT_DIR / f"mi_equipo_{date.today().isoformat()}.csv"
        mi_equipo_df.to_csv(mi_equipo_file, index=False, encoding="utf-8-sig")
        print(f"Mi equipo ({len(mi_equipo_df)}/{len(mis_jugadores)} encontrados): {mi_equipo_file}")

        historico_equipo_file = OUT_DIR / "historico_mi_equipo.csv"
        if historico_equipo_file.exists():
            hist_eq = pd.read_csv(historico_equipo_file, dtype=str)
            hist_eq = hist_eq[hist_eq["fecha"] != date.today().isoformat()]
            combinado_eq = pd.concat([hist_eq, mi_equipo_df], ignore_index=True)
        else:
            combinado_eq = mi_equipo_df
        combinado_eq.to_csv(historico_equipo_file, index=False, encoding="utf-8-sig")

        resumen = build_telegram_summary_mi_equipo(mi_equipo_df, len(mi_equipo_df), len(mis_jugadores))
    else:
        resumen = build_telegram_summary(df)

    limpia_csv_antiguos()
    send_telegram_message(resumen)


if __name__ == "__main__":
    main()