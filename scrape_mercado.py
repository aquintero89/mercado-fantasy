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
    """Verifica la coincidencia de nombre (por palabras completas) y opcionalmente de equipo."""
    n_target_norm = normaliza(nombre_target)
    n_df_norm = normaliza(nombre_df)
    
    tokens_target = n_target_norm.split()
    tokens_df = n_df_norm.split()
    
    # Comprobar si todas las palabras buscadas están presentes como palabras completas en el nombre
    match_nombre = all(t in tokens_df for t in tokens_target) or all(t in tokens_target for t in tokens_df)
    
    if not match_nombre:
        return False
        
    # Si se especificó equipo en mis_jugadores.txt, debe coincidir con el del mercado
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
