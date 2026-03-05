import os
import re
import difflib
import json
from flask import Flask, request, Response
from supabase import create_client

app = Flask(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

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

def normalize(text):
    if not text:
        return ""
    text = str(text).upper().strip()
    for a, b in [('Á','A'),('É','E'),('Í','I'),('Ó','O'),('Ú','U'),('Ü','U'),('Ñ','N')]:
        text = text.replace(a, b)
    text = re.sub(r'[^\w\s@/.]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    tokens = [SYNONYM_MAP.get(t, t) for t in text.split()]
    return ' '.join(t for t in tokens if t)

def extract_numbers(text):
    return set(re.findall(r'\d+(?:[.,]\d+)?', text))

def build_catalog_entry(row):
    name = row.get('DescCortaArt', '') or ''
    atribs = []
    for i in range(4, 9):
        k = row.get(f'ATRIBUTO{i}', '') or ''
        v = row.get(f'ValorAtrib{i}', '') or ''
        if k or v:
            atribs.append(f"{k} {v}")
    enriched = ' '.join([name, name, name, ' '.join(atribs)])
    return {
        'code':      row.get('CodigoArt', ''),
        'name':      name,
        'price':     str(row.get('Precio', '') or ''),
        'norm':      normalize(enriched),
        'norm_name': normalize(name),
        'numbers':   extract_numbers(normalize(name)),
    }

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

    # NIVEL 3: Overlap alto de palabras
    name_words = set(norm_name.split())
    overlap = len(q_words & name_words) / max(len(q_words), len(name_words)) if q_words and name_words else 0

    number_bonus = 0
    if q_numbers and entry['numbers']:
        matched = q_numbers & entry['numbers']
        number_bonus = len(matched) / max(len(q_numbers), 1) * 0.15

    code_bonus = 0.20 if entry['code'] and normalize(entry['code']) in norm_q else 0

    if overlap >= 0.6:
        return min(0.70 + overlap * 0.20 + number_bonus + code_bonus, 1.0)

    # NIVEL 4: Fuzzy como último recurso
    seq_name = difflib.SequenceMatcher(None, norm_q, norm_name).ratio()
    seq_full = difflib.SequenceMatcher(None, norm_q, norm_full).ratio()
    score = seq_name * 0.45 + seq_full * 0.20 + overlap * 0.20 + number_bonus + code_bonus
    return min(score, 1.0)

def best_match(query, catalog):
    scored = [(score_match(query, entry), i, entry) for i, entry in enumerate(catalog)]
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, _, best_entry = scored[0]
    return best_entry, round(best_score, 3)

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
    best_col, best_avg = None, 0
    for col in columns:
        values = [str(r.get(col, '') or '') for r in rows[:10]]
        avg_len = sum(len(v) for v in values) / max(len(values), 1)
        if avg_len > best_avg:
            best_avg = avg_len
            best_col = col
    return best_col

@app.route('/match', methods=['POST'])
def match_products():
    try:
        data = request.get_json()
        if not data:
            return Response(json.dumps({"error": "No se recibieron datos"}, ensure_ascii=False), status=400, mimetype='application/json')

        client_products = (
            data.get('rows') or data.get('productos') or
            data.get('items') or data.get('articulos') or
            data.get('products') or []
        )

        if not client_products:
            return Response(json.dumps({"error": "No se encontraron productos"}, ensure_ascii=False), status=400, mimetype='application/json')

        desc_column = detect_description_column(client_products)
        if not desc_column:
            return Response(json.dumps({"error": "No se pudo detectar columna de descripcion"}, ensure_ascii=False), status=400, mimetype='application/json')

        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        resp = supabase.table("products").select(
            "CodigoArt, DescCortaArt, Precio, "
            "ATRIBUTO4, ValorAtrib4, ATRIBUTO5, ValorAtrib5, "
            "ATRIBUTO6, ValorAtrib6, ATRIBUTO7, ValorAtrib7, "
            "ATRIBUTO8, ValorAtrib8"
        ).execute()
        catalog_raw = resp.data

        if not catalog_raw:
            return Response(json.dumps({"error": "El catalogo esta vacio"}, ensure_ascii=False), status=400, mimetype='application/json')

        catalog = [build_catalog_entry(row) for row in catalog_raw]

        resultados = []
        for item in client_products:
            descripcion = str(item.get(desc_column, '') or '').strip()
            if not descripcion:
                continue
            match, score = best_match(descripcion, catalog)
            resultados.append({
                "original_text":        descripcion,
                "descripcion_original": descripcion,
                "codigo":               match['code'],
                "nombre_catalogo":      match['name'],
                "precio":               match['price'],
                "confianza":            score,
                "requiere_revision":    score < 0.7
            })

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
        return Response(json.dumps({"error": str(e)}, ensure_ascii=False), status=500, mimetype='application/json')

@app.route('/health', methods=['GET'])
def health():
    return Response(json.dumps({"status": "ok"}), status=200, mimetype='application/json')

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
