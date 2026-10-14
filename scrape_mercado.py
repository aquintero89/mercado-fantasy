"""
Scraper diario del mercado de La Liga Fantasy (analiticafantasy.com)
=====================================================================

Qué hace:
- Abre la página del mercado con un navegador headless (la tabla se pagina
  con JavaScript) y recorre TODAS las páginas en una sola pasada. La tabla ya
  incluye subidas y bajadas; el signo (+/-) lo decide el valor, no la pestaña.
- Extrae: jugador, posición, equipo, precio, subida/bajada en € y en %.
- Guarda mercado_YYYY-MM-DD.csv, historico.csv e historico_mi_equipo.csv.
- Filtra tus jugadores (mis_jugadores.txt) y envía el resumen a Telegram.
- Si algo falla, te avisa por Telegram con el motivo.
- Borra los CSV de días anteriores.

Dependencias (el workflow ya las instala):
    pip install playwright pandas requests lxml html5lib beautifulsoup4
    playwright install chromium
"""

import io
import os
import random
import re
import sys
import unicodedata
from datetime import date
from html import escape
from urllib.parse import urlparse
from pathlib import Path

import pandas as pd
import requests
from playwright.sync_api import sync_playwright

URL = os.environ.get(
    "MERCADO_URL", "https://www.analiticafantasy.com/fantasy-la-liga/mercado"
)
OUT_DIR = Path(__file__).parent
POSITIONS = ["PT", "DF", "MC", "DL", "DT"]  # DT = entrenador
MAX_PAGINAS = 80  # el mercado ronda las 59 páginas
ESPERA_TABLA_MS = int(os.environ.get("ESPERA_TABLA_MS", "45000"))
# Pausa media entre páginas (con variación aleatoria): imita a una persona
PAUSA_PAGINAS_MS = int(os.environ.get("PAUSA_PAGINAS_MS", "1500"))
# NAVEGADOR_VISIBLE=1 -> Chrome real con ventana (usar con xvfb-run en servidores)
NAVEGADOR_VISIBLE = os.environ.get("NAVEGADOR_VISIBLE", "") == "1"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TOP_N = 10

MIS_JUGADORES_FILE = OUT_DIR / "mis_jugadores.txt"
# Marca con la fecha de la última captura completa (para saltar reintentos)
MARCA_COMPLETA = OUT_DIR / "ultima_captura_completa.txt"


# --------------------------------------------------------------------------
# Nombres y coincidencias
# --------------------------------------------------------------------------
def normaliza(texto: str) -> str:
    """Quita tildes/mayúsculas para comparar nombres de forma flexible."""
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return texto.lower().strip()


def parse_entrada_jugador(linea: str):
    """Permite 'Nombre' o 'Nombre (Equipo)' para desambiguar nombres genéricos."""
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", linea.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return linea.strip(), None


def _es_inicial(tok: str) -> bool:
    """'d.' o 'd' (una sola letra, con o sin punto)."""
    return len(tok.rstrip(".")) == 1


def _tokens_coinciden(a: str, b: str) -> bool:
    if a == b:
        return True
    if _es_inicial(a) and b.startswith(a[0]):
        return True
    if _es_inicial(b) and a.startswith(b[0]):
        return True
    return False


def _cubre(pocos: list, muchos: list) -> bool:
    """Cada palabra de 'pocos' tiene una palabra distinta en 'muchos'
    (una inicial como 'd.' cubre a 'diego')."""
    libres = list(muchos)
    for p in pocos:
        for g in libres:
            if _tokens_coinciden(p, g):
                libres.remove(g)
                break
        else:
            return False
    return True


def puntua_coincidencia(nombre_lista: str, nombre_mercado: str):
    """Puntuación de coincidencia por palabras completas (así 'Rodri' no
    coincide con 'Rodríguez'), o None si no hay coincidencia válida.
      4 = idéntico                    ('Marcos Alonso' == 'Marcos Alonso')
      3 = igual salvo iniciales       ('Diego Villares' ~ 'D. Villares')
      2 = tú abreviaste               ('Yamal' dentro de 'Lamine Yamal')
      1 = el mercado abrevió          ('Antonio Blanco' -> el mercado muestra 'Blanco')
    """
    pl = normaliza(nombre_lista).split()
    pm = normaliza(nombre_mercado).split()
    if not pl or not pm:
        return None
    if set(pl) == set(pm):
        return 4
    if len(pl) == len(pm) and _cubre(pl, pm) and _cubre(pm, pl):
        return 3
    if len(pl) <= len(pm) and _cubre(pl, pm):
        return 2
    if len(pm) <= len(pl) and _cubre(pm, pl):
        return 1
    return None


def carga_mis_jugadores() -> list:
    if not MIS_JUGADORES_FILE.exists():
        return []
    with open(MIS_JUGADORES_FILE, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def filtra_mi_equipo(df: pd.DataFrame, entradas: list):
    """Para cada jugador de tu lista elige el MEJOR candidato del mercado
    (exacto > tú abreviaste > el mercado abrevió). Con 'Nombre (Equipo)' solo
    considera candidatos de ese equipo.
    Devuelve (dataframe_encontrados, lista_de_no_encontrados)."""
    if not entradas:
        return df.iloc[0:0], []

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
            ambiguos.append(f"{linea} -> {nombres}")

        indices_elegidos.append(mejores[0][1])

    if ambiguos:
        print("  Coincidencias ambiguas (añade el equipo entre paréntesis para fijarlas):")
        for a in ambiguos:
            print(f"    {a}")

    vistos = set()
    unicos = [i for i in indices_elegidos if not (i in vistos or vistos.add(i))]
    return df.loc[unicos], no_encontrados


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram no configurado (falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID). Omitido.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=20)
    except requests.RequestException as e:
        print(f"Error de red enviando a Telegram: {e}")
        return
    if resp.status_code == 200:
        print("Resumen enviado a Telegram.")
    else:
        print(f"Error enviando a Telegram: {resp.status_code} {resp.text}")


def _fmt(n) -> str:
    return f"{int(n):,}".replace(",", ".")


def _fmt_precio(valor) -> str:
    try:
        return _fmt(float(valor))
    except (TypeError, ValueError):
        return "?"


def build_telegram_summary(df: pd.DataFrame, aviso: str = "") -> str:
    hoy = date.today().isoformat()
    lines = [f"<b>📊 Mercado Fantasy — {hoy}</b>", ""]
    if aviso:
        lines += [aviso, ""]

    df = df.copy()
    df["cambio_eur_num"] = pd.to_numeric(df["cambio_eur"], errors="coerce")

    subidas = df[df["cambio_eur_num"] >= 0].sort_values("cambio_eur_num", ascending=False).head(TOP_N)
    if not subidas.empty:
        lines.append("<b>🟢 Top subidas</b>")
        for _, r in subidas.iterrows():
            lines.append(f"↑ {r['jugador']} ({r['equipo']}): +{_fmt(r['cambio_eur_num'])} € ({r['cambio_pct']}%)")
        lines.append("")

    bajadas = df[df["cambio_eur_num"] < 0].sort_values("cambio_eur_num", ascending=True).head(TOP_N)
    if not bajadas.empty:
        lines.append("<b>🔴 Top bajadas</b>")
        for _, r in bajadas.iterrows():
            lines.append(f"↓ {r['jugador']} ({r['equipo']}): {_fmt(r['cambio_eur_num'])} € ({r['cambio_pct']}%)")

    return "\n".join(lines)


def build_telegram_summary_mi_equipo(df: pd.DataFrame, encontrados: int, total: int,
                                     no_encontrados=None, aviso: str = "",
                                     captura_incompleta: bool = False) -> str:
    hoy = date.today().isoformat()
    df = df.copy()
    df["cambio_eur_num"] = pd.to_numeric(df["cambio_eur"], errors="coerce")

    es_subida = df["cambio_eur_num"] >= 0
    n_subidas = int(es_subida.sum())
    n_bajadas = int((~es_subida).sum())
    suma_subidas = df.loc[es_subida, "cambio_eur_num"].sum()
    suma_bajadas = df.loc[~es_subida, "cambio_eur_num"].sum()
    balance_total = df["cambio_eur_num"].sum()
    signo_balance = "+" if balance_total >= 0 else ""

    lines = [
        f"<b>⚽ Mi equipo — {hoy}</b> ({encontrados}/{total} encontrados)",
        "",
    ]
    if aviso:
        lines += [aviso, ""]
    lines += [
        f"🟢 Subidas: {n_subidas} (+{_fmt(suma_subidas)} €)   🔴 Bajadas: {n_bajadas} ({_fmt(suma_bajadas)} €)",
        f"💰 Balance total: {signo_balance}{_fmt(balance_total)} €",
        "",
    ]

    df = df.sort_values("cambio_eur_num", ascending=False)
    for _, r in df.iterrows():
        cambio = r["cambio_eur_num"]
        sube = pd.notna(cambio) and cambio >= 0
        punto = "🟢" if sube else "🔴"
        flecha = "↑" if sube else "↓"
        signo = "+" if sube else ""
        cambio_txt = f"{signo}{_fmt(cambio)} €" if pd.notna(cambio) else "?"
        lines.append(
            f"{punto} {flecha} <b>{r['jugador']}</b> ({r['equipo']}): {cambio_txt} "
            f"({r['cambio_pct']}%) — precio: {_fmt_precio(r['precio_eur'])} €"
        )
    if len(df) == 0:
        lines.append("(No se encontró ningún jugador de mis_jugadores.txt en el mercado de hoy)")
    if no_encontrados:
        etiqueta = "Sin datos hoy (no se leyó todo el mercado)" if captura_incompleta else "No encontrados"
        lines += ["", f"❓ {etiqueta}: " + ", ".join(escape(n, quote=False) for n in no_encontrados)]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Parseo de celdas
# --------------------------------------------------------------------------
def limpia_prefijo_duplicado(nombre: str) -> str:
    """A veces el texto trae basura delante: un número de ranking pegado
    ('16Koski') o 1-3 mayúsculas duplicadas ('SSSaba Sazonov', 'FAFacu')."""
    nombre = re.sub(r"^\d+", "", nombre).strip()
    m = re.match(r"^([A-ZÁÉÍÓÚÑ]{1,3})([A-ZÁÉÍÓÚÑ][a-záéíóúñ].*)$", nombre)
    if m:
        return m.group(2)
    return nombre


def split_jugador_cell(text: str):
    """La celda 'Jugador' llega como 'NombreDFEquipo' pegado: separamos por el
    código de posición."""
    for pos in POSITIONS:
        idx = text.find(pos)
        if idx != -1:
            nombre = limpia_prefijo_duplicado(text[:idx].strip())
            equipo = text[idx + len(pos):].strip()
            return nombre, pos, equipo
    return limpia_prefijo_duplicado(text.strip()), "", ""


def parse_change_cell(text: str):
    """'+2.645.168 €+3,6%' / '-993.000 €-2,1%' -> (valor_eur, pct)"""
    text = text.replace("\xa0", " ")
    m_val = re.search(r"([+-]?[\d.]+)\s*€", text)
    m_pct = re.search(r"([+-]?[\d,]+)\s*%", text)
    valor = m_val.group(1).replace(".", "") if m_val else None
    pct = m_pct.group(1).replace(",", ".") if m_pct else None
    return valor, pct


def parse_precio_cell(text: str):
    m = re.search(r"([\d.]+)\s*€", text)
    return m.group(1).replace(".", "") if m else None


# --------------------------------------------------------------------------
# Navegador
# --------------------------------------------------------------------------
def leer_tabla_actual(page):
    """Lee la tabla más grande de la página. Prueba lxml y, si no está
    instalado, bs4+html5lib."""
    try:
        html = page.content()
    except Exception:
        return None
    for flavor in ("lxml", "bs4"):
        try:
            tablas = pd.read_html(io.StringIO(html), flavor=flavor)
        except (ImportError, ValueError):
            continue
        if tablas:
            return max(tablas, key=lambda t: t.shape[0])
    return None


def detecta_columnas(table):
    cols = {str(c).strip().lower(): c for c in table.columns}
    col_j = next((c for k, c in cols.items() if "jugador" in k), None)
    col_c = next((c for k, c in cols.items() if "subida" in k or "bajada" in k), None)
    col_p = next((c for k, c in cols.items() if "precio" in k), None)
    if col_j is None or col_c is None:
        return None
    return col_j, col_c, col_p


INDICADORES_BLOQUEO = (
    "you have been blocked", "access denied", "request blocked",
    "attention required", "just a moment", "verify you are human", "captcha",
)


def texto_visible(page, limite=400) -> str:
    try:
        return re.sub(r"\s+", " ", page.inner_text("body", timeout=5000))[:limite]
    except Exception:
        return ""


def detecta_bloqueo(page):
    texto = texto_visible(page, 2000).lower()
    return next((i for i in INDICADORES_BLOQUEO if i in texto), None)


JS_LIMPIA_OVERLAYS = """() => {
  document.querySelectorAll('[id^="qc-cmp2"]').forEach(e => e.remove());
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed') continue;
    const r = el.getBoundingClientRect();
    if (r.width >= innerWidth * 0.9 && r.height >= innerHeight * 0.9 && !el.querySelector('table')) el.remove();
  }
  document.documentElement.style.overflow = '';
  document.body.style.overflow = '';
  if (location.hash.includes('google_vignette')) {
    history.replaceState(null, '', location.pathname + location.search);
  }
}"""


def quita_banner_cookies(page):
    """Quita banners de cookies y anuncios a pantalla completa que tapan la
    página e interceptan los clics."""
    try:
        page.evaluate(JS_LIMPIA_OVERLAYS)
    except Exception:
        pass


def abrir_pagina_con_reintentos(page, intentos=3):
    """Abre la web SIN esperar 'networkidle' (los anuncios hacen que nunca
    llegue) y espera a que aparezca la tabla. Devuelve (ok, diagnóstico)."""
    diag = ""
    for intento in range(1, intentos + 1):
        print(f"Abriendo {URL} (intento {intento}/{intentos}) ...")
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            diag = f"error de navegación: {(str(e).splitlines() or ['?'])[0][:150]}"
            print(f"  {diag}")
            page.wait_for_timeout(3000 * intento)
            continue

        quita_banner_cookies(page)

        bloqueo = detecta_bloqueo(page)
        if bloqueo:
            diag = f"la web devolvió una página de bloqueo ('{bloqueo}'): {texto_visible(page, 200)}"
            print(f"  {diag}")
            page.wait_for_timeout(4000 * intento)
            continue

        try:
            page.wait_for_selector("table", timeout=ESPERA_TABLA_MS)
            page.wait_for_timeout(1500)
            return True, ""
        except Exception:
            try:
                titulo = page.title()
            except Exception:
                titulo = "?"
            diag = f"no apareció la tabla. Título: '{titulo}'. Texto: '{texto_visible(page, 250)}'"
            print(f"  {diag}")
    return False, diag


def leer_total_esperado(page):
    """La web indica '584 jugadores en mercado': sirve para comprobar que
    capturamos todo."""
    m = re.search(r"([\d.]+)\s+jugadores en mercado", texto_visible(page, 100000))
    return int(m.group(1).replace(".", "")) if m else None


# Marca el botón 'Siguiente' más cercano a la tabla (puede haber otros 'Siguiente'
# en anuncios o carruseles). Devuelve 0 = no hay botón, 1 = activo, 2 = desactivado.
JS_MARCA_SIGUIENTE = """() => {
  document.querySelectorAll('[data-sig]').forEach(e => e.removeAttribute('data-sig'));
  const tablas = [...document.querySelectorAll('table')];
  if (!tablas.length) return 0;
  tablas.sort((a, b) => b.rows.length - a.rows.length);
  const tb = tablas[0].getBoundingClientRect();
  const cands = [...document.querySelectorAll('button, a, [role=button], span, div, li, p')]
    .filter(e => e.textContent.trim() === 'Siguiente'
              && ![...e.children].some(c => c.textContent.trim() === 'Siguiente'));
  if (!cands.length) return 0;
  const dist = e => { const r = e.getBoundingClientRect();
    return Math.abs(r.top - tb.bottom) + Math.abs((r.left + r.width / 2) - (tb.left + tb.width / 2)) * 0.1; };
  cands.sort((a, b) => dist(a) - dist(b));
  cands[0].setAttribute('data-sig', '1');
  const btn = cands[0].closest('button') || cands[0];
  return (btn.disabled || btn.getAttribute('aria-disabled') === 'true') ? 2 : 1;
}"""


def click_siguiente(page, modo: str = "js") -> str:
    """Pulsa 'Siguiente'. Devuelve 'ok', 'desactivado' o 'no_hay'.
    modo 'js' hace el clic desde JavaScript (no lo frena ningún anuncio encima);
    modo 'raton' hace un clic de ratón normal."""
    quita_banner_cookies(page)
    try:
        estado = page.evaluate(JS_MARCA_SIGUIENTE)
    except Exception:
        estado = 0

    if estado == 2:
        return "desactivado"
    if estado == 1:
        try:
            if modo == "js":
                page.evaluate("() => { const e = document.querySelector('[data-sig]');"
                              " (e.closest('button') || e).click(); }")
            else:
                page.locator("[data-sig]").first.click(force=True, timeout=5000)
            return "ok"
        except Exception:
            return "no_hay"

    # Plan B: búsqueda por texto
    boton = page.get_by_text("Siguiente", exact=True)
    if boton.count() == 0:
        return "no_hay"
    primero = boton.first
    try:
        if not primero.is_enabled():
            return "desactivado"
    except Exception:
        pass
    try:
        primero.click(force=True, timeout=5000)
        return "ok"
    except Exception:
        try:
            primero.evaluate("el => el.click()")
            return "ok"
        except Exception:
            return "no_hay"


def firma_tabla(table) -> str:
    return "|".join(table.astype(str).to_numpy().ravel())


def avanza_pagina(page, table_actual):
    """Pulsa 'Siguiente' y espera a que la tabla cambie de verdad; si no
    cambia, reintenta con otro tipo de clic. Devuelve (tabla_nueva, motivo):
    motivo '' si avanzó; 'fin_normal' (botón desactivado), 'sin_boton' o
    'sin_cambio' si no se pudo avanzar."""
    firma_actual = firma_tabla(table_actual)
    for modo in ("js", "raton", "js"):
        resultado = click_siguiente(page, modo)
        if resultado == "desactivado":
            return None, "fin_normal"
        if resultado == "no_hay":
            return None, "sin_boton"
        for _ in range(12):  # hasta ~6 s esperando el cambio
            page.wait_for_timeout(500)
            nueva = leer_tabla_actual(page)
            if nueva is not None and not nueva.empty and firma_tabla(nueva) != firma_actual:
                page.wait_for_timeout(300)  # que termine de pintarse
                estable = leer_tabla_actual(page)
                return (estable if estable is not None and not estable.empty else nueva), ""
        try:
            page.keyboard.press("Escape")  # cierra posibles ventanas emergentes
        except Exception:
            pass
    return None, "sin_cambio"


def procesa_tabla(table, rows: list) -> bool:
    """Convierte las filas de la tabla en registros. False si no reconoce las columnas."""
    columnas = detecta_columnas(table)
    if columnas is None:
        print(f"  No reconozco las columnas de la tabla: {list(table.columns)}")
        return False
    col_jugador, col_cambio, col_precio = columnas

    for _, row in table.iterrows():
        jugador_raw = str(row.get(col_jugador, ""))
        cambio_raw = str(row.get(col_cambio, ""))
        precio_raw = str(row.get(col_precio, "")) if col_precio is not None else ""

        nombre, posicion, equipo = split_jugador_cell(jugador_raw)
        valor, pct = parse_change_cell(cambio_raw)
        precio = parse_precio_cell(precio_raw)

        if not nombre or not posicion:
            continue  # fila no reconocida

        # El signo real lo decide el valor, no la pestaña de origen
        valor_num = float(valor) if valor not in (None, "") else 0.0
        rows.append({
            "fecha": date.today().isoformat(),
            "tipo": "Subidas" if valor_num >= 0 else "Bajadas",
            "jugador": nombre,
            "posicion": posicion,
            "equipo": equipo,
            "precio_eur": precio,
            "cambio_eur": valor,
            "cambio_pct": pct,
        })
    return True


def scrape_mercado(page, rows: list):
    """Recorre todas las páginas de la tabla (vista por defecto).
    Devuelve (páginas_leídas, motivo_de_parada, texto_de_bloqueo_o_None)."""
    table = leer_tabla_actual(page)
    leidas = 0
    motivo = "fin_normal" if table is not None else "sin_tabla"

    while table is not None and not table.empty and leidas < MAX_PAGINAS:
        if not procesa_tabla(table, rows):
            motivo = "columnas_desconocidas"
            break
        leidas += 1
        print(f"  Página {leidas}: {len(table)} filas (total acumulado: {len(rows)})")
        table, motivo = avanza_pagina(page, table)
        if table is not None and PAUSA_PAGINAS_MS > 0:
            page.wait_for_timeout(int(PAUSA_PAGINAS_MS * random.uniform(0.6, 1.4)))

    if leidas >= MAX_PAGINAS:
        motivo = "limite_paginas"
        print(f"  Límite de seguridad de {MAX_PAGINAS} páginas alcanzado.")

    bloqueo = None
    if motivo not in ("fin_normal", ""):
        bloqueo = detecta_bloqueo(page)
        if bloqueo:
            print(f"  La web bloqueó el acceso tras {leidas} páginas ('{bloqueo}'): {texto_visible(page, 200)}")
    print(f"  Fin del recorrido: {leidas} páginas, motivo: {motivo}")
    return leidas, motivo, bloqueo


def crea_contexto(browser):
    """Contexto con User-Agent normal (sin 'HeadlessChrome', que algunas webs
    bloquean) y sin la marca de automatización."""
    tmp = browser.new_context()
    ua = tmp.new_page().evaluate("navigator.userAgent")
    tmp.close()
    contexto = browser.new_context(
        user_agent=ua.replace("HeadlessChrome", "Chrome"),
        viewport={"width": 1280, "height": 720},
        locale="es-ES",
        timezone_id="Europe/Madrid",
    )
    contexto.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return contexto


# --------------------------------------------------------------------------
# Archivos
# --------------------------------------------------------------------------
def limpia_csv_antiguos():
    """Borra los CSV de días anteriores y deja solo los de hoy."""
    hoy = date.today().isoformat()
    for patron in ["mercado_*.csv", "mi_equipo_*.csv"]:
        for f in OUT_DIR.glob(patron):
            if hoy not in f.name:
                f.unlink()
                print(f"  Borrado archivo antiguo: {f.name}")


def guarda_csv_del_dia(ruta: Path, df: pd.DataFrame, completa: bool):
    """Una pasada incompleta no machaca el archivo de una pasada completa."""
    if not completa and ruta.exists():
        print(f"  Se conserva {ruta.name} (la captura de hoy está incompleta).")
        return
    df.to_csv(ruta, index=False, encoding="utf-8-sig")


def acumula_historico(nombre_archivo: str, df: pd.DataFrame, completa: bool = True):
    ruta = OUT_DIR / nombre_archivo
    hoy = date.today().isoformat()
    if ruta.exists():
        hist = pd.read_csv(ruta, dtype=str)
        de_hoy = hist[hist["fecha"] == hoy]
        resto = hist[hist["fecha"] != hoy]
        if completa:
            df = pd.concat([resto, df], ignore_index=True)
        else:
            # Pasada incompleta: conservamos lo de hoy y solo añadimos lo nuevo
            ya = set(zip(de_hoy["jugador"], de_hoy["equipo"]))
            nuevos = df[[(j, e) not in ya for j, e in zip(df["jugador"], df["equipo"])]]
            df = pd.concat([resto, de_hoy, nuevos], ignore_index=True)
    df.to_csv(ruta, index=False, encoding="utf-8-sig")
    print(f"Histórico actualizado: {nombre_archivo} ({len(df)} filas totales)")


def send_telegram_photo(png: bytes, caption: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000], "parse_mode": "HTML"},
            files={"photo": ("pantalla.png", png, "image/png")},
            timeout=30,
        )
        print("Captura de pantalla enviada a Telegram." if resp.status_code == 200
              else f"Error enviando la captura: {resp.status_code} {resp.text[:150]}")
    except requests.RequestException as e:
        print(f"Error de red enviando la captura: {e}")


def prepara_diagnostico(page, motivo: str, fallos: list):
    """Texto + captura de pantalla de lo que ve el navegador en este momento."""
    host = urlparse(page.url).netloc if page.url else ""
    propios = [(s, u) for s, u in fallos if host and host in u]
    lineas = [f"🔎 <b>Diagnóstico</b> — motivo de parada: {escape(motivo, quote=False)}"]
    if propios:
        lineas.append("Peticiones de la web con error: " + escape(
            "; ".join(f"{s} {u[:80]}" for s, u in propios[-4:]), quote=False))
    lineas.append("Texto en pantalla: " + escape(texto_visible(page, 220), quote=False))
    try:
        foto = page.screenshot(type="png")
    except Exception:
        foto = None
    return "\n".join(lineas), foto


def envia_diagnostico(texto: str, foto):
    if foto:
        send_telegram_photo(foto, texto)
    else:
        send_telegram_message(texto)


def captura_de_hoy_completa() -> bool:
    try:
        return MARCA_COMPLETA.read_text().strip() == date.today().isoformat()
    except OSError:
        return False


def fallar(motivo: str, diagnostico=None):
    """Avisa por Telegram (para enterarte desde el móvil) y termina con error."""
    print(motivo)
    send_telegram_message(
        "⚠️ <b>Mercado Fantasy</b>: el scraper falló hoy.\n" + escape(motivo[:700], quote=False)
        + "\n\nPuedes reintentar desde GitHub → Actions → Run workflow."
    )
    if diagnostico:
        envia_diagnostico(*diagnostico)
    sys.exit(1)


def lanza_navegador(p):
    """Con NAVEGADOR_VISIBLE=1 intenta Chrome real con ventana (necesita
    pantalla: xvfb-run en servidores); si no puede, usa Chromium headless."""
    args = ["--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"]
    if NAVEGADOR_VISIBLE:
        for extra in ({"channel": "chrome"}, {}):
            try:
                navegador = p.chromium.launch(headless=False, args=args, **extra)
                print("Navegador: " + ("Chrome real" if extra else "Chromium") + " con ventana.")
                return navegador
            except Exception as e:
                print(f"  No se pudo abrir el navegador con ventana ({extra or 'chromium'}): "
                      f"{(str(e).splitlines() or ['?'])[0][:120]}")
    print("Navegador: Chromium headless.")
    return p.chromium.launch(headless=True, args=args)


def main():
    # En los reintentos programados: si ya hay captura completa de hoy, no repetir
    if os.environ.get("SALTAR_SI_COMPLETA") == "1" and captura_de_hoy_completa():
        print("Ya hay una captura completa de hoy: no hace falta repetir.")
        return

    rows = []
    esperados = None
    bloqueo = None
    leidas = 0

    with sync_playwright() as p:
        browser = lanza_navegador(p)
        page = crea_contexto(browser).new_page()
        fallos = []  # peticiones que devuelven error (403, 429...)
        page.on("response", lambda r: fallos.append((r.status, r.url)) if r.status >= 400 else None)

        ok, diag = abrir_pagina_con_reintentos(page)
        if not ok:
            diagnostico = prepara_diagnostico(page, "no_carga_tabla", fallos)
            browser.close()
            fallar(f"No se pudo cargar la tabla tras varios intentos. {diag}", diagnostico)

        esperados = leer_total_esperado(page)
        if esperados:
            print(f"La web indica {esperados} jugadores en mercado.")

        print("Extrayendo mercado completo...")
        leidas, motivo_parada, bloqueo = scrape_mercado(page, rows)
        diagnostico = None
        parada_rara = motivo_parada not in ("fin_normal", "")
        corta = bool(esperados) and len(rows) < 0.95 * esperados
        if bloqueo or parada_rara or corta:
            diagnostico = prepara_diagnostico(page, motivo_parada, fallos)
            if not bloqueo:
                relevantes = [s for s, u in fallos if s in (403, 429) and urlparse(page.url).netloc in u]
                if relevantes:
                    bloqueo = f"respuestas {relevantes[-1]} de la propia web"
        browser.close()

    if not rows:
        fallar("La tabla cargó pero no se extrajo ningún jugador (¿cambió la estructura de la web?).")

    df = pd.DataFrame(rows).drop_duplicates(subset=["fecha", "jugador", "equipo"])
    print(f"\nTotal de jugadores únicos capturados hoy: {len(df)}")

    completa = not bloqueo and not (esperados and len(df) < 0.95 * esperados)
    aviso = ""
    if not completa:
        total_txt = f" de {esperados}" if esperados else ""
        if bloqueo:
            aviso = f"⚠️ La web bloqueó el acceso tras {leidas} páginas: solo se leyeron {len(df)}{total_txt} jugadores."
        else:
            aviso = f"⚠️ Captura incompleta: {len(df)}{total_txt} jugadores."
        print(aviso)

    if completa:
        MARCA_COMPLETA.write_text(date.today().isoformat())
    guarda_csv_del_dia(OUT_DIR / f"mercado_{date.today().isoformat()}.csv", df, completa)
    acumula_historico("historico.csv", df, completa)

    mis_jugadores = carga_mis_jugadores()
    if mis_jugadores:
        mi_equipo_df, no_encontrados = filtra_mi_equipo(df, mis_jugadores)
        encontrados = len(mis_jugadores) - len(no_encontrados)
        guarda_csv_del_dia(OUT_DIR / f"mi_equipo_{date.today().isoformat()}.csv", mi_equipo_df, completa)
        print(f"Mi equipo: {encontrados}/{len(mis_jugadores)} encontrados")
        if no_encontrados:
            print(f"  No encontrados en el mercado de hoy: {', '.join(no_encontrados)}")
        acumula_historico("historico_mi_equipo.csv", mi_equipo_df, completa)

        resumen = build_telegram_summary_mi_equipo(
            mi_equipo_df, encontrados, len(mis_jugadores), no_encontrados, aviso,
            captura_incompleta=not completa)
    else:
        print("No hay 'mis_jugadores.txt' (o está vacío): resumen general del mercado.")
        resumen = build_telegram_summary(df, aviso)

    limpia_csv_antiguos()
    send_telegram_message(resumen)
    if diagnostico and not completa:
        envia_diagnostico(*diagnostico)


if __name__ == "__main__":
    main()
