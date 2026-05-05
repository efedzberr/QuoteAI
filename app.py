import os
import re
import difflib
import json
import unicodedata
from collections import Counter
from flask import Flask, request, Response
from supabase import create_client

app = Flask(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# ---------------------------------------------------------------------------
# Sinónimos de unidades técnicas
# ---------------------------------------------------------------------------
UNIT_SYNONYMS = {
    'KW': ['KW', 'KILOWATT', 'KILOWATTS'],
    'HP': ['HP', 'HORSEPOWER', 'CABALLOS'],
    'A':  ['A', 'AMP', 'AMPS', 'AMPERE', 'AMPERES', 'AMPER'],
    'V':  ['V', 'VOLT', 'VOLTS', 'VOLTIOS'],
    'HZ': ['HZ', 'HERTZ', 'HERCIOS'],
    'MM': ['MM', 'MILIMETRO', 'MILIMETROS'],
    'PZ': ['PZ', 'PZA', 'PIEZA', 'PIEZAS'],
    '3P': ['3P', 'TRIPOLAR', 'TRIFASICO', 'TRI', '3 POLOS', '3POLOS'],
    '2P': ['2P', 'BIPOLAR', 'BIFASICO', '2 POLOS', '2POLOS'],
}
SYNONYM_MAP = {v: k for k, variants in UNIT_SYNONYMS.items() for v in variants}

# ---------------------------------------------------------------------------
# Palabras vacías — se ignoran en la búsqueda por palabras clave
# ---------------------------------------------------------------------------
STOPWORDS = {
    'DE', 'DEL', 'LA', 'LAS', 'EL', 'LOS', 'UN', 'UNA', 'UNOS', 'UNAS',
    'Y', 'O', 'E', 'U', 'A', 'PARA', 'POR', 'CON', 'SIN', 'EN', 'AL',
    'SE', 'NO', 'SI', 'QUE', 'SU', 'MAS', 'ES', 'SON', 'THE', 'OF',
    'AND', 'OR', 'FOR', 'TO', 'IN', 'AT', 'BY',
}

# ---------------------------------------------------------------------------
# Posibles nombres de columna donde puede venir el código del producto
# ---------------------------------------------------------------------------
CODE_COLUMN_NAMES = [
    'codigoart', 'codigo', 'code', 'sku', 'clave',
    'cve', 'cve_art', 'cveart', 'no_parte', 'noparte',
    'numparte', 'partnumber', 'part_number', 'item_code',
    'itemcode', 'product_code', 'productcode', 'ref', 'referencia'
]


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------
def strip_accents(text):
    """
    Quita acentos y diacríticos latinos de cualquier texto (mayúsculas o
    minúsculas). Usa NFKD para descomponer caracteres como 'é' -> 'e' + tilde
    y luego elimina las marcas combinantes. La 'ñ' se trata aparte porque en
    español es una letra distinta de 'n', no una 'n' acentuada.
    """
    if text is None:
        return ""
    text = str(text)
    # Tratar Ñ/ñ explícitamente antes de la descomposición Unicode
    text = text.replace('Ñ', 'N').replace('ñ', 'n')
    # NFKD descompone los demás caracteres acentuados; luego filtramos las marcas
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


def normalize(text):
    if not text:
        return ""
    text = strip_accents(text).upper().strip()
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    tokens = [SYNONYM_MAP.get(t, t) for t in text.split()]
    return ' '.join(t for t in tokens if t)


def normalize_code(code):
    """Normaliza un código de producto: mayúsculas, sin espacios ni símbolos."""
    if code is None:
        return ""
    code = str(code).strip().upper()
    # Quitar espacios internos y caracteres no alfanuméricos comunes (guiones, puntos)
    code = re.sub(r'[\s\-\.]+', '', code)
    return code


def extract_keywords(norm_text, min_len=3):
    """Extrae palabras significativas eliminando stopwords y palabras muy cortas."""
    return [w for w in norm_text.split() if w not in STOPWORDS and len(w) >= min_len]


# ---------------------------------------------------------------------------
# Detección y extracción del código del producto
# ---------------------------------------------------------------------------
def detect_code_column(rows):
    """Busca una columna que parezca contener el código del producto."""
    if not rows:
        return None
    columns = list(rows[0].keys())
    for col in columns:
        col_lower = col.lower().replace(' ', '').replace('_', '')
        for candidate in CODE_COLUMN_NAMES:
            cand_clean = candidate.replace('_', '')
            if col_lower == cand_clean or cand_clean in col_lower:
                return col
    return None


def extract_code_from_text(text):
    """
    Intenta extraer un código de producto del texto.
    Busca tokens que parezcan códigos: predominantemente numéricos
    o alfanuméricos con al menos 4 caracteres, sin ser palabras comunes.
    Retorna el primer candidato razonable encontrado.
    """
    if not text:
        return None
    text = str(text).strip()

    # Patrón 1: secuencia de 5+ dígitos (formato típico de CodigoArt en este catálogo)
    m = re.search(r'\b(\d{5,})\b', text)
    if m:
        return m.group(1)

    # Patrón 2: alfanumérico con letras y números, 4-20 caracteres
    # (ej. "AB-1234", "X100", "SKU-9876")
    candidates = re.findall(r'\b([A-Z]+[\-\.]?\d+[A-Z0-9\-\.]*)\b', text.upper())
    if candidates:
        # Filtrar candidatos que sean unidades técnicas comunes
        excluded = {'3P', '2P', '1P', 'KW', 'HP', 'HZ', 'MM', 'PZ',
                    'V', 'A', '24V', '110V', '220V', '440V', '60HZ', '50HZ'}
        for c in candidates:
            clean = re.sub(r'[\-\.]', '', c)
            if clean not in excluded and len(clean) >= 4:
                return c

    return None


# ---------------------------------------------------------------------------
# Construcción de entrada de catálogo
# ---------------------------------------------------------------------------
def build_catalog_entry(row):
    name = row.get('DescCortaArt', '') or ''
    norm = normalize(name)
    code = row.get('CodigoArt', '') or ''
    return {
        'code':      code,
        'norm_code': normalize_code(code),
        'name':      name,
        'price':     str(row.get('Precio', '') or ''),
        'norm':      norm,
        'norm_set':  set(norm.split()),
    }


# ---------------------------------------------------------------------------
# PASADA 0 — Búsqueda directa por código en Supabase
# ---------------------------------------------------------------------------
def code_lookup(code, supabase, catalog_by_code=None):
    """
    Busca un producto por su CodigoArt directamente.
    1) Primero intenta en el catálogo en memoria (más rápido).
    2) Si no encuentra, consulta Supabase con coincidencia exacta.
    Retorna el entry del producto o None si no existe.
    """
    if not code:
        return None

    norm = normalize_code(code)
    if not norm:
        return None

    # 1) Intentar en memoria primero
    if catalog_by_code and norm in catalog_by_code:
        return catalog_by_code[norm]

    # 2) Consulta directa a Supabase (eq = igualdad exacta)
    try:
        resp = supabase.table("products").select(
            "CodigoArt, DescCortaArt, Precio"
        ).eq("CodigoArt", code).limit(1).execute()
        rows = resp.data or []
        if rows:
            row = rows[0]
            return {
                'code':      row.get('CodigoArt', ''),
                'norm_code': normalize_code(row.get('CodigoArt', '')),
                'name':      row.get('DescCortaArt', ''),
                'price':     str(row.get('Precio', '') or ''),
                'norm':      normalize(row.get('DescCortaArt', '')),
            }
    except Exception:
        pass

    # 3) Último intento: si el código original tenía caracteres especiales,
    #    probar con la versión normalizada (sin guiones/espacios)
    if norm != str(code).strip().upper():
        try:
            resp = supabase.table("products").select(
                "CodigoArt, DescCortaArt, Precio"
            ).eq("CodigoArt", norm).limit(1).execute()
            rows = resp.data or []
            if rows:
                row = rows[0]
                return {
                    'code':      row.get('CodigoArt', ''),
                    'norm_code': normalize_code(row.get('CodigoArt', '')),
                    'name':      row.get('DescCortaArt', ''),
                    'price':     str(row.get('Precio', '') or ''),
                    'norm':      normalize(row.get('DescCortaArt', '')),
                }
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# PASADA 1 — Fuzzy Match en memoria (SequenceMatcher + Jaccard)
# ---------------------------------------------------------------------------
def fuzzy_match(query, catalog):
    norm_q  = normalize(query)
    q_words = set(norm_q.split())

    scored = []
    for entry in catalog:
        norm_e  = entry['norm']
        e_words = entry['norm_set']

        seq     = difflib.SequenceMatcher(None, norm_q, norm_e).ratio()
        overlap = (len(q_words & e_words) / max(len(q_words), len(e_words))
                   if q_words and e_words else 0.0)
        combined = 0.5 * seq + 0.5 * overlap
        scored.append((combined, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_entry = scored[0]
    return best_entry, round(best_score, 3)


# ---------------------------------------------------------------------------
# PASADA 2 — Intersección de palabras clave en catálogo en memoria
# ---------------------------------------------------------------------------
def keyword_ilike_match(query, catalog, supabase=None):
    """
    Intersección de palabras clave hecha en memoria sobre el catálogo ya
    cargado y normalizado (sin acentos). Esto reemplaza la versión anterior
    basada en ILIKE de Supabase, que NO era insensible a acentos cuando el
    catálogo guardaba descripciones con tildes (p.ej. "Eléctrica").

    Para cada palabra clave significativa:
      1. Recorre el catálogo y marca cada producto cuyo nombre normalizado
         contenga la keyword como substring.
      2. Cuenta en cuántas keywords aparece cada producto (intersección).
      3. El ganador es el producto con mayor número de coincidencias.
      4. En caso de empate, desempata con fuzzy score contra la query.

    El parámetro `supabase` se mantiene por compatibilidad pero ya no se usa.
    Devuelve: (mejor_entry, keywords_coincidentes, total_keywords)
    """
    norm_q   = normalize(query)
    keywords = extract_keywords(norm_q)

    if not keywords:
        return None, 0, 0

    code_counter = Counter()

    for kw in keywords:
        for entry in catalog:
            # 'norm' ya está sin acentos y en mayúsculas, así que el substring
            # match es totalmente insensible a acentos y a may/min.
            if kw in entry['norm']:
                code_counter[entry['code']] += 1

    if not code_counter:
        return None, 0, 0

    # Indexar el catálogo por código una sola vez para resolver el ganador
    catalog_by_code = {e['code']: e for e in catalog if e['code']}

    total_keywords = len(keywords)
    max_count = code_counter.most_common(1)[0][1]
    candidates = [c for c, count in code_counter.items() if count == max_count]

    if len(candidates) == 1:
        best_code = candidates[0]
    else:
        best_code = max(
            candidates,
            key=lambda c: difflib.SequenceMatcher(
                None, norm_q, catalog_by_code[c]['norm']
            ).ratio()
        )

    best_entry = catalog_by_code.get(best_code)
    if best_entry is None:
        return None, 0, 0

    # Devolvemos el entry con el formato esperado por match_product
    return {
        'code':  best_entry['code'],
        'name':  best_entry['name'],
        'price': best_entry['price'],
        'norm':  best_entry['norm'],
    }, max_count, total_keywords


# ---------------------------------------------------------------------------
# Lógica principal de matching (tres pasadas)
# ---------------------------------------------------------------------------
def match_product(query, code, catalog, catalog_by_code, catalog_by_norm, supabase):
    """
    Pasada 0: Búsqueda por código (si se proporcionó).
      - Match exacto → confianza = 1.0, requiere_revision = False
    Pasada 1: Match exacto por descripción normalizada (sin acentos, sin puntuación).
      - Match exacto → confianza = 1.0, requiere_revision = False
    Pasada 2: Fuzzy en memoria sobre la descripción.
      - score >= 0.70 → resultado directo, requiere_revision = False
    Pasada 3: Intersección de palabras clave en catálogo en memoria.
      - Confianza proporcional escalada entre 0.40 y 0.69
      - Si ninguna keyword encuentra nada → NO ENCONTRADO
    """
    # --- Pasada 0: Código del producto ---
    code_to_try = code
    if not code_to_try and query:
        # Si no hay código explícito, intentar extraerlo del texto
        code_to_try = extract_code_from_text(query)

    if code_to_try:
        match0 = code_lookup(code_to_try, supabase, catalog_by_code)
        if match0:
            return {
                "codigo":            match0['code'],
                "nombre_catalogo":   match0['name'],
                "precio":            match0['price'],
                "confianza":         1.0,
                "requiere_revision": False,
                "metodo":            "codigo",
            }

    # Si no hay descripción para hacer fallback, retornar NO ENCONTRADO
    if not query or not query.strip():
        return {
            "codigo":            "NO ENCONTRADO",
            "nombre_catalogo":   "NO ENCONTRADO",
            "precio":            "",
            "confianza":         0.0,
            "requiere_revision": True,
            "metodo":            "ninguno",
        }

    # --- Pasada 1: Match exacto por descripción normalizada ---
    # Si la descripción del cliente, una vez normalizada (sin acentos, sin
    # puntuación, sin espacios extra, mayúsculas, sinónimos de unidades
    # aplicados), coincide letra por letra con la descripción normalizada de
    # algún producto del catálogo, lo damos por bueno con confianza 1.0.
    norm_query = normalize(query)
    if norm_query and norm_query in catalog_by_norm:
        match_exact = catalog_by_norm[norm_query]
        return {
            "codigo":            match_exact['code'],
            "nombre_catalogo":   match_exact['name'],
            "precio":            match_exact['price'],
            "confianza":         1.0,
            "requiere_revision": False,
            "metodo":            "exacto",
        }

    # --- Pasada 2: Fuzzy ---
    match1, score1 = fuzzy_match(query, catalog)
    if score1 >= 0.70:
        return {
            "codigo":            match1['code'],
            "nombre_catalogo":   match1['name'],
            "precio":            match1['price'],
            "confianza":         score1,
            "requiere_revision": False,
            "metodo":            "fuzzy",
        }

    # --- Pasada 3: Intersección de keywords en catálogo en memoria ---
    match2, matched_kw, total_kw = keyword_ilike_match(query, catalog, supabase)

    if match2 is None or matched_kw == 0:
        return {
            "codigo":            "NO ENCONTRADO",
            "nombre_catalogo":   "NO ENCONTRADO",
            "precio":            "",
            "confianza":         0.0,
            "requiere_revision": True,
            "metodo":            "ninguno",
        }

    # Mínimo de keywords coincidentes según tamaño de la query
    if total_kw >= 5:
        min_required = 3
    elif total_kw >= 3:
        min_required = 2
    else:
        min_required = 1

    if matched_kw < min_required:
        return {
            "codigo":            "NO ENCONTRADO",
            "nombre_catalogo":   "NO ENCONTRADO",
            "precio":            "",
            "confianza":         0.0,
            "requiere_revision": True,
            "metodo":            "ninguno",
        }

    ratio      = matched_kw / total_kw
    confidence = round(0.40 + ratio * 0.29, 3)

    return {
        "codigo":            match2['code'],
        "nombre_catalogo":   match2['name'],
        "precio":            match2['price'],
        "confianza":         confidence,
        "requiere_revision": True,
        "metodo":            "keyword",
    }


# ---------------------------------------------------------------------------
# Detección automática de columna de descripción
# ---------------------------------------------------------------------------
def detect_description_column(rows):
    if not rows:
        return None
    priority_names = [
        'descripcion', 'description', 'producto', 'product',
        'item', 'articulo', 'article', 'nombre', 'name',
        'desc', 'material', 'concepto', 'detalle'
    ]
    columns = list(rows[0].keys())
    for col in columns:
        if any(p in col.lower() for p in priority_names):
            return col
    # Fallback: columna con valor promedio más largo
    best_col, best_avg = None, 0
    for col in columns:
        values  = [str(r.get(col, '') or '') for r in rows[:10]]
        avg_len = sum(len(v) for v in values) / max(len(values), 1)
        if avg_len > best_avg:
            best_avg = avg_len
            best_col = col
    return best_col


# ---------------------------------------------------------------------------
# Rutas Flask
# ---------------------------------------------------------------------------
@app.route('/match', methods=['POST'])
def match_products():
    try:
        data = request.get_json()
        if not data:
            return Response(
                json.dumps({"error": "No se recibieron datos"}, ensure_ascii=False),
                status=400, mimetype='application/json'
            )

        client_products = (
            data.get('rows') or data.get('productos') or
            data.get('items') or data.get('articulos') or
            data.get('products') or []
        )

        if not client_products:
            return Response(
                json.dumps({"error": "No se encontraron productos"}, ensure_ascii=False),
                status=400, mimetype='application/json'
            )

        desc_column = detect_description_column(client_products)
        code_column = detect_code_column(client_products)

        if not desc_column and not code_column:
            return Response(
                json.dumps({"error": "No se pudo detectar columna de descripcion ni de codigo"},
                           ensure_ascii=False),
                status=400, mimetype='application/json'
            )

        # Conexión a Supabase
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

        # Cargar catálogo completo — IMPORTANTE: PostgREST (la capa que usa
        # Supabase por debajo) limita las respuestas a 1000 filas por defecto.
        # supabase-py no pagina automáticamente, así que tenemos que hacerlo
        # manualmente con .range(start, end). Si no, los productos cuyos id
        # caen fuera de las primeras 1000 filas nunca se cargan al catálogo
        # en memoria y por lo tanto nunca pueden matchear como exacto/fuzzy.
        PAGE_SIZE = 1000
        catalog_raw = []
        offset = 0
        while True:
            resp = supabase.table("products").select(
                "CodigoArt, DescCortaArt, Precio, "
                "ATRIBUTO4, ValorAtrib4, ATRIBUTO5, ValorAtrib5, "
                "ATRIBUTO6, ValorAtrib6, ATRIBUTO7, ValorAtrib7, "
                "ATRIBUTO8, ValorAtrib8"
            ).range(offset, offset + PAGE_SIZE - 1).execute()

            page = resp.data or []
            if not page:
                break

            catalog_raw.extend(page)

            # Si la página vino con menos de PAGE_SIZE filas, es la última.
            if len(page) < PAGE_SIZE:
                break

            offset += PAGE_SIZE

            # Salvaguarda contra bucles infinitos (catálogos enormes o bugs).
            if offset > 100000:
                break

        if not catalog_raw:
            return Response(
                json.dumps({"error": "El catalogo esta vacio"}, ensure_ascii=False),
                status=400, mimetype='application/json'
            )

        catalog = [build_catalog_entry(row) for row in catalog_raw]

        # Índice de catálogo por código normalizado (para lookup O(1))
        catalog_by_code = {
            entry['norm_code']: entry
            for entry in catalog
            if entry['norm_code']
        }

        # Índice de catálogo por descripción normalizada (para match exacto O(1)).
        # Si dos productos del catálogo tienen exactamente la misma descripción
        # normalizada, conservamos el primero (en la práctica son duplicados o
        # variantes, y el match exacto no debería resolver ambigüedades).
        catalog_by_norm = {}
        for entry in catalog:
            if entry['norm'] and entry['norm'] not in catalog_by_norm:
                catalog_by_norm[entry['norm']] = entry

        # Procesar cada producto del cliente
        resultados = []
        for item in client_products:
            descripcion = (
                str(item.get(desc_column, '') or '').strip()
                if desc_column else ''
            )
            codigo_in = (
                str(item.get(code_column, '') or '').strip()
                if code_column else ''
            )

            # Si no hay ni descripción ni código, omitir el renglón
            if not descripcion and not codigo_in:
                continue

            cant = (
                item.get('Cant') or item.get('cant') or
                item.get('cantidad') or item.get('qty') or
                item.get('quantity') or '1'
            )
            unid = (
                item.get('Unid') or item.get('unid') or
                item.get('unidad') or item.get('uom') or
                item.get('unit') or 'PZA'
            )

            result = match_product(
                descripcion, codigo_in,
                catalog, catalog_by_code, catalog_by_norm, supabase
            )

            resultados.append({
                "original_text":        descripcion,
                "descripcion_original": descripcion,
                "codigo_original":      codigo_in,
                "codigo":               result["codigo"],
                "nombre_catalogo":      result["nombre_catalogo"],
                "precio":               result["precio"],
                "confianza":            result["confianza"],
                "requiere_revision":    result["requiere_revision"],
                "metodo":               result["metodo"],
                "cant":                 cant,
                "unid":                 unid,
            })

        response_data = {
            "lines":              resultados,
            "total":              len(resultados),
            "requieren_revision": sum(1 for r in resultados if r['requiere_revision']),
            "columna_descripcion": desc_column,
            "columna_codigo":      code_column,
        }
        json_str = json.dumps(response_data, ensure_ascii=False)
        return Response(json_str, status=200, mimetype='application/json')

    except Exception as e:
        return Response(
            json.dumps({"error": str(e)}, ensure_ascii=False),
            status=500, mimetype='application/json'
        )


@app.route('/health', methods=['GET'])
def health():
    return Response(json.dumps({"status": "ok"}), status=200, mimetype='application/json')


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

