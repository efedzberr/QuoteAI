import os
import re
import difflib
import json
from flask import Flask, request, Response
from supabase import create_client

app = Flask(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# ---------------------------------------------------------------------------
# NORMALIZACIÓN
# ---------------------------------------------------------------------------

UNIT_SYNONYMS = {
    'KW': ['KW', 'KILOWATT', 'KILOWATTS'],
    'HP': ['HP', 'HORSEPOWER', 'CABALLOS'],
    'A': ['A', 'AMP', 'AMPS', 'AMPERE', 'AMPERES', 'AMPER'],
    'V': ['V', 'VOLT', 'VOLTS', 'VOLTIOS'],
    'HZ': ['HZ', 'HERTZ', 'HERCIOS'],
    'MM': ['MM', 'MILIMETRO', 'MILIMETROS'],
    'PZ': ['PZ', 'PZA', 'PIEZA', 'PIEZAS'],
    '3P': ['3P', 'TRIPOLAR', 'TRIFASICO', 'TRI', '3 POLOS', '3POLOS'],
    '2P': ['2P', 'BIPOLAR', 'BIFASICO', '2 POLOS', '2POLOS'],
}

def build_synonym_map():
    m = {}
    for canonical, variants in UNIT_SYNONYMS.items():
        for v in variants:
            m[v] = canonical
    return m

SYNONYM_MAP = build_synonym_map()

def normalize(text):
    if not text:
        return ""
    text = str(text).upper().strip()
    # Reemplazar tildes
    for a, b in [('Á','A'),('É','E'),('Í','I'),('Ó','O'),('Ú','U'),('Ü','U'),('Ñ','N')]:
        text = text.replace(a, b)
    # Eliminar puntuación excepto @, / y .
    text = re.sub(r'[^\w\s@/.]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    # Normalizar sinónimos de unidades
    tokens = text.split()
    tokens = [SYNONYM_MAP.get(t, t) for t in tokens]
    return ' '.join(tokens)

def extract_numbers(text):
    """Extrae todos los números y valores numéricos del texto."""
    return set(re.findall(r'\d+(?:[.,]\d+)?', text))

def extract_tokens(text):
    """Extrae tokens significativos (longitud >= 2)."""
    return set(t for t in normalize(text).split() if len(t) >= 2)

# ---------------------------------------------------------------------------
# CONSTRUCCIÓN DEL ÍNDICE DE CATÁLOGO
# ---------------------------------------------------------------------------

def build_catalog_entry(row):
    """
    Construye un texto de búsqueda enriquecido combinando todos los campos
    relevantes del catálogo con pesos implícitos por repetición.
    """
    # Campos con sus pesos (repetición = mayor peso en scoring)
    name        = row.get('DescCortaArt') or row.get('ProductName', '')
    desc        = row.get('DescLargaArt') or row.get('Description', '')
    code        = row.get('CodigoArt') or row.get('ProductCode', '')
    category    = row.get('CategoriaArt') or row.get('Category', '')
    subcategory = row.get('SubCategoriaArt', '')
    dept        = row.get('DeptoArt', '')
    brand       = row.get('Marca') or row.get('Manufacturer', '')

    # Atributos dinámicos (pares ATRIBUTO/ValorAtrib del 4 al 8)
    atribs = []
    for i in range(4, 9):
        k = row.get(f'ATRIBUTO{i}', '')
        v = row.get(f'ValorAtrib{i}', '')
        if k or v:
            atribs.append(f"{k} {v}")

    # Texto enriquecido: nombre repetido 3x para darle mayor peso
    enriched = ' '.join([
        name, name, name,          # peso alto
        desc, desc,                # peso medio
        code,                      # código exacto
        category, subcategory,     # contexto
        dept, brand,
        ' '.join(atribs)
    ])

    return {
        'code':     code,
        'name':     name,
        'price':    str(row.get('Precio') or row.get('UnitPrice', '')),
        'unit':     row.get('UMP') or row.get('UnitOfMeasure', ''),
        'brand':    brand,
        'category': category,
        'norm':     normalize(enriched),
        'norm_name': normalize(name),
        'norm_desc': normalize(desc),
        'numbers':  extract_numbers(normalize(name + ' ' + desc)),
        'tokens':   extract_tokens(name + ' ' + desc + ' ' + ' '.join(atribs)),
    }

# ---------------------------------------------------------------------------
# ALGORITMO DE MATCHING MULTI-CAMPO
# ---------------------------------------------------------------------------

def score_match(query, entry):
    norm_q    = normalize(query)
    norm_name = entry['norm_name']
    norm_full = entry['norm']
    q_words   = set(norm_q.split())
    q_numbers = extract_numbers(norm_q)

    # NIVEL 1: Coincidencia exacta
    if norm_q == norm_name:
        return 1.0

    # NIVEL 2: Uno contiene al otro
    if norm_q in norm_name or norm_name in norm_q:
        len_ratio = min(len(norm_q), len(norm_name)) / max(len(norm_q), len(norm_name))
        return 0.90 + (len_ratio * 0.09)

    # NIVEL 3: Overlap de palabras clave
    name_words = set(norm_name.split())
    overlap = len(q_words & name_words) / max(len(q_words), len(name_words)) if q_words and name_words else 0

    number_bonus = 0
    if q_numbers and entry['numbers']:
        matched = q_numbers & entry['numbers']
        number_bonus = len(matched) / max(len(q_numbers), 1) * 0.15

    code_bonus = 0.20 if entry['code'] and normalize(entry['code']) in norm_q else 0

    if overlap >= 0.6:
        return min(0.70 + overlap * 0.20 + number_bonus + code_bonus, 1.0)

    # NIVEL 4: Fuzzy como ultimo recurso
    seq_name = difflib.SequenceMatcher(None, norm_q, norm_name).ratio()
    seq_full = difflib.SequenceMatcher(None, norm_q, norm_full).ratio()

    score = (
        seq_name * 0.45 +
        seq_full * 0.20 +
        overlap  * 0.20 +
        number_bonus +
        code_bonus
    )
    return min(score, 1.0)

def best_match(query, catalog):
    scored = [(score_match(query, entry), entry) for entry in catalog]
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_entry = scored[0]
    return best_entry, round(best_score, 3)

# ---------------------------------------------------------------------------
# DETECCIÓN DE COLUMNA DE DESCRIPCIÓN
# ---------------------------------------------------------------------------

def detect_description_column(rows):
    if not rows:
        return None

    priority_names = [
        'descripcion', 'description', 'producto', 'product',
        'item', 'articulo', 'article', 'nombre', 'name',
        'desc', 'material', 'concepto', 'detalle'
    ]

    first_row = rows[0]
    columns = list(first_row.keys())

    for col in columns:
        if any(p in col.lower() for p in priority_names):
            return col

    # Fallback: columna con texto más largo en promedio
    best_col, best_avg = None, 0
    for col in columns:
        values = [str(r.get(col, '') or '') for r in rows[:10]]
        avg_len = sum(len(v) for v in values) / max(len(values), 1)
        if avg_len > best_avg:
            best_avg = avg_len
            best_col = col

    return best_col

# ---------------------------------------------------------------------------
# ENDPOINT /match
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
                json.dumps({"error": "No se encontraron productos en el request"}, ensure_ascii=False),
                status=400, mimetype='application/json'
            )

        desc_column = detect_description_column(client_products)
        if not desc_column:
            return Response(
                json.dumps({"error": "No se pudo detectar la columna de descripcion"}, ensure_ascii=False),
                status=400, mimetype='application/json'
            )

        # Cargar catálogo completo desde Supabase
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
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

        # Construir índice enriquecido
        catalog = [build_catalog_entry(row) for row in catalog_raw]

        # Matching
        resultados = []
        for item in client_products:
            descripcion = str(item.get(desc_column, '') or '').strip()
            if not descripcion:
                continue

            match, score = best_match(descripcion, catalog)
            resultados.append({
                "original_text":      descripcion,
                "descripcion_original": descripcion,
                "codigo":             match['code'],
                "nombre_catalogo":    match['name'],
                "precio":             match['price'],
                "confianza":          score,
                "requiere_revision":  score < 0.7
            })

        # Construir JSON como string para evitar que Make lo desestructure
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


@app.route('/catalog-sample', methods=['GET'])
def catalog_sample():
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        resp = supabase.table("products").select("CodigoArt, DescCortaArt").limit(20).execute()
        return Response(
            json.dumps({"total": len(resp.data), "muestra": resp.data}, ensure_ascii=False),
            status=200, mimetype='application/json'
        )
    except Exception as e:
        return Response(
            json.dumps({"error": str(e)}, ensure_ascii=False),
            status=500, mimetype='application/json'
        )


@app.route('/search-test', methods=['GET'])
def search_test():
    """Busca productos específicos en Supabase por nombre."""
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        terms = [
            'Placa Venecia',
            'Atenuador',
            'Lampara Noche Puerquito',
            'Apagador Sencillo Roma',
            'Abrazadera',
            'Cuchillas Repuesto Cutter',
        ]
        resultados = {}
        for term in terms:
            resp = supabase.table("products").select("CodigoArt, DescCortaArt") \
                .ilike("DescCortaArt", f"%{term}%").limit(3).execute()
            resultados[term] = resp.data
        return Response(
            json.dumps(resultados, ensure_ascii=False),
            status=200, mimetype='application/json'
        )
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
