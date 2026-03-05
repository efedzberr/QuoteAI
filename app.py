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
# Construcción del catálogo
# ---------------------------------------------------------------------------
def build_catalog_entry(row):
    name = row.get('DescCortaArt', '') or ''
    norm = normalize(name)
    return {
        'code':      row.get('CodigoArt', ''),
        'name':      name,
        'price':     str(row.get('Precio', '') or ''),
        'norm':      norm,
        'norm_set':  set(norm.split()),   # set pre-calculado para intersección rápida
    }


# ---------------------------------------------------------------------------
# PASADA 1 — Fuzzy Match (SequenceMatcher + Jaccard, igual que match2.py)
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
# PASADA 2 — Intersección de palabras clave
# ---------------------------------------------------------------------------
def keyword_intersection_match(query, catalog):
    """
    Para cada palabra clave significativa de la query:
      - Recoge los códigos de todos los productos que contienen esa palabra.
    Luego cuenta en cuántas búsquedas aparece cada código.
    El ganador es el código que aparece en más búsquedas (máxima intersección).
    """
    norm_q   = normalize(query)
    keywords = extract_keywords(norm_q)

    if not keywords:
        return None, 0, 0

    # Contar en cuántas keywords aparece cada código
    code_counter = Counter()
    for kw in keywords:
        for entry in catalog:
            if kw in entry['norm_set']:
                code_counter[entry['code']] += 1

    if not code_counter:
        return None, 0, 0

    best_code, best_count = code_counter.most_common(1)[0]
    best_entry = next((e for e in catalog if e['code'] == best_code), None)

    return best_entry, best_count, len(keywords)


# ---------------------------------------------------------------------------
# Lógica principal de matching (dos pasadas)
# ---------------------------------------------------------------------------
def match_product(query, catalog):
    """
    Pasada 1: Fuzzy Match.
      - score >= 0.70 → resultado directo, no requiere revisión.
    Pasada 2: Intersección de palabras clave.
      - Confianza proporcional a palabras coincidentes (escala 0.40–0.69).
      - Si ninguna palabra coincide → NO ENCONTRADO.
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

    # --- Pasada 2: Intersección de keywords ---
    match2, matched_kw, total_kw = keyword_intersection_match(query, catalog)

    if match2 is None or matched_kw == 0:
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
        "metodo":            f"keywords_{matched_kw}_de_{total_kw}",
        "requiere_revision": True,  # siempre revisar resultados de pasada 2
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

        # Cargar catálogo desde Supabase
        supabase    = create_client(SUPABASE_URL, SUPABASE_KEY)
        resp        = supabase.table("products").select(
            "CodigoArt, DescCortaArt, Precio, ATRIBUTO4, ValorAtrib4, ATRIBUTO5, ValorAtrib5, ATRIBUTO6, ValorAtrib6, ATRIBUTO7, ValorAtrib7, ATRIBUTO8, ValorAtrib8"
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

            result = match_product(descripcion, catalog)
            resultados.append({
                "original_text":        descripcion,
                "descripcion_original": descripcion,
                "codigo":               result["codigo"],
                "nombre_catalogo":      result["nombre_catalogo"],
                "precio":               result["precio"],
                "confianza":            result["confianza"],
                "requiere_revision":    result["requiere_revision"],
            })

        # Respuesta en formato compatible con Make
        items_json = ','.join([json.dumps(r, ensure_ascii=False) for r in resultados])
        json_str = (
            '{"lines":['
            + items_json
            + '],"total":' + str(len(resultados))
            + ',"requieren_revision":' + str(sum(1 for r in resultados if r['requiere_revision']))
            + ',"columna_detectada":"' + desc_column + '"}'
        )
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
