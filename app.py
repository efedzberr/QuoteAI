import os
import re
import difflib
import json
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
# Normalización
# ---------------------------------------------------------------------------
def normalize(text):
    if not text:
        return ""
    text = str(text).upper().strip()
    for a, b in [('Á','A'),('É','E'),('Í','I'),('Ó','O'),('Ú','U'),('Ü','U'),('Ñ','N')]:
        text = text.replace(a, b)
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    tokens = [SYNONYM_MAP.get(t, t) for t in text.split()]
    return ' '.join(t for t in tokens if t)


def extract_keywords(norm_text, min_len=3):
    """Extrae palabras significativas eliminando stopwords y palabras muy cortas."""
    return [w for w in norm_text.split() if w not in STOPWORDS and len(w) >= min_len]


# ---------------------------------------------------------------------------
# Construcción de entrada de catálogo
# ---------------------------------------------------------------------------
def build_catalog_entry(row):
    name = row.get('DescCortaArt', '') or ''
    norm = normalize(name)
    return {
        'code':     row.get('CodigoArt', ''),
        'name':     name,
        'price':    str(row.get('Precio', '') or ''),
        'norm':     norm,
        'norm_set': set(norm.split()),
    }


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
# PASADA 2 — Intersección de palabras clave via ILIKE en Supabase
# ---------------------------------------------------------------------------
def keyword_ilike_match(query, supabase):
    """
    Para cada palabra clave significativa:
      1. Hace SELECT con ILIKE '%keyword%' en Supabase (igual que tu SQL manual)
      2. Recoge los CodigoArt que devuelve cada búsqueda
      3. Cuenta en cuántas búsquedas aparece cada código (intersección)
      4. El ganador es el código con mayor número de coincidencias
      5. En caso de empate, desempata con fuzzy score contra la query
    Devuelve: (mejor_entry, keywords_coincidentes, total_keywords)
    """
    norm_q   = normalize(query)
    keywords = extract_keywords(norm_q)

    if not keywords:
        return None, 0, 0

    # Por cada keyword → ILIKE query a Supabase
    code_counter  = Counter()   # código → cuántas keywords lo encontraron
    code_to_entry = {}          # código → datos del producto

    for kw in keywords:
        try:
            resp = supabase.table("products").select(
                "CodigoArt, DescCortaArt, Precio"
            ).ilike("DescCortaArt", f"%{kw}%").execute()

            for row in (resp.data or []):
                code = row.get('CodigoArt', '')
                if code:
                    code_counter[code] += 1
                    if code not in code_to_entry:
                        code_to_entry[code] = {
                            'code':  code,
                            'name':  row.get('DescCortaArt', ''),
                            'price': str(row.get('Precio', '') or ''),
                            'norm':  normalize(row.get('DescCortaArt', '')),
                        }
        except Exception:
            # Si falla una keyword, continuamos con las demás
            continue

    if not code_counter:
        return None, 0, 0

    total_keywords = len(keywords)

    # Encontrar el máximo de coincidencias
    max_count = code_counter.most_common(1)[0][1]

    # Candidatos con el mismo máximo → desempatar con fuzzy score
    candidates = [
        code for code, count in code_counter.items()
        if count == max_count
    ]

    if len(candidates) == 1:
        best_code  = candidates[0]
        best_count = max_count
    else:
        # Desempate: el que tenga mayor similitud fuzzy con la query original
        norm_q = normalize(query)
        best_code  = max(
            candidates,
            key=lambda c: difflib.SequenceMatcher(
                None, norm_q, code_to_entry[c]['norm']
            ).ratio()
        )
        best_count = max_count

    best_entry = code_to_entry.get(best_code)
    return best_entry, best_count, total_keywords


# ---------------------------------------------------------------------------
# Lógica principal de matching (dos pasadas)
# ---------------------------------------------------------------------------
def match_product(query, catalog, supabase):
    """
    Pasada 1: Fuzzy en memoria.
      - score >= 0.70 → resultado directo, requiere_revision = False
    Pasada 2: Intersección ILIKE en Supabase.
      - Confianza proporcional escalada entre 0.40 y 0.69
      - Si ninguna keyword encuentra nada → NO ENCONTRADO (nunca basura)
    """
    # --- Pasada 1: Fuzzy ---
    match1, score1 = fuzzy_match(query, catalog)
    if score1 >= 0.70:
        return {
            "codigo":            match1['code'],
            "nombre_catalogo":   match1['name'],
            "precio":            match1['price'],
            "confianza":         score1,
            "requiere_revision": False,
        }

    # --- Pasada 2: ILIKE en Supabase ---
    match2, matched_kw, total_kw = keyword_ilike_match(query, supabase)

    if match2 is None or matched_kw == 0:
        return {
            "codigo":            "NO ENCONTRADO",
            "nombre_catalogo":   "NO ENCONTRADO",
            "precio":            "",
            "confianza":         0.0,
            "requiere_revision": True,
        }

    # Mínimo 2 keywords coincidentes si la query tiene 3 o más keywords.
    # Evita devolver productos basura que solo coinciden en 1 palabra genérica.
    min_required = 2 if total_kw >= 3 else 1
    if matched_kw < min_required:
        return {
            "codigo":            "NO ENCONTRADO",
            "nombre_catalogo":   "NO ENCONTRADO",
            "precio":            "",
            "confianza":         0.0,
            "requiere_revision": True,
        }

    # Confianza proporcional escalada entre 0.40 y 0.69
    ratio      = matched_kw / total_kw
    confidence = round(0.40 + ratio * 0.29, 3)

    return {
        "codigo":            match2['code'],
        "nombre_catalogo":   match2['name'],
        "precio":            match2['price'],
        "confianza":         confidence,
        "requiere_revision": True,   # siempre revisar resultados de pasada 2
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
        if not desc_column:
            return Response(
                json.dumps({"error": "No se pudo detectar columna de descripcion"}, ensure_ascii=False),
                status=400, mimetype='application/json'
            )

        # Conexión a Supabase — se reutiliza para Pasada 1 y Pasada 2
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

        # Cargar catálogo completo para Pasada 1 (fuzzy en memoria)
        resp = supabase.table("products").select(
            "CodigoArt, DescCortaArt, Precio, "
            "ATRIBUTO4, ValorAtrib4, ATRIBUTO5, ValorAtrib5, "
            "ATRIBUTO6, ValorAtrib6, ATRIBUTO7, ValorAtrib7, "
            "ATRIBUTO8, ValorAtrib8"
        ).execute()
        catalog_raw = resp.data

        if not catalog_raw:
            return Response(
                json.dumps({"error": "El catalogo esta vacio"}, ensure_ascii=False),
                status=400, mimetype='application/json'
            )

        catalog = [build_catalog_entry(row) for row in catalog_raw]

        # Procesar cada producto del cliente
        resultados = []
        for item in client_products:
            descripcion = str(item.get(desc_column, '') or '').strip()
            if not descripcion:
                continue

            # Leer cantidad y unidad del input original (varios nombres posibles)
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

            # Pasamos supabase a match_product para que Pasada 2 pueda usarlo
            result = match_product(descripcion, catalog, supabase)
            resultados.append({
                "original_text":        descripcion,
                "descripcion_original": descripcion,
                "codigo":               result["codigo"],
                "nombre_catalogo":      result["nombre_catalogo"],
                "precio":               result["precio"],
                "confianza":            result["confianza"],
                "requiere_revision":    result["requiere_revision"],
                "cant":                 cant,
                "unid":                 unid,
            })

        # Respuesta en formato compatible con Make
        # json.dumps garantiza que lines sea array de objetos reales, no strings
        response_data = {
            "lines":              resultados,
            "total":              len(resultados),
            "requieren_revision": sum(1 for r in resultados if r['requiere_revision']),
            "columna_detectada":  desc_column
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
