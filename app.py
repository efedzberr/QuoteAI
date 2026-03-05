import os
import re
import csv
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
    """Normalize text: uppercase, remove accents, remove punctuation,
    collapse spaces, and apply unit synonyms — from match2.py logic."""
    if not text:
        return ""
    text = str(text).upper().strip()
    for a, b in [('Á','A'),('É','E'),('Í','I'),('Ó','O'),('Ú','U'),('Ü','U'),('Ñ','N')]:
        text = text.replace(a, b)
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    tokens = [SYNONYM_MAP.get(t, t) for t in text.split()]
    return ' '.join(t for t in tokens if t)


def build_catalog_entry(row):
    """Build enriched catalog entry including attribute fields."""
    name = row.get('DescCortaArt', '') or ''
    atribs = []
    for i in range(4, 9):
        k = row.get(f'ATRIBUTO{i}', '') or ''
        v = row.get(f'ValorAtrib{i}', '') or ''
        if k or v:
            atribs.append(f"{k} {v}")
    # Enrich norm with name repeated 3x + attributes (keeps catalog name dominant)
    enriched = ' '.join([name, name, name, ' '.join(atribs)])
    return {
        'code':  row.get('CodigoArt', ''),
        'name':  name,
        'price': str(row.get('Precio', '') or ''),
        'norm':  normalize(enriched),      # enriched norm used for scoring
        'norm_name': normalize(name),      # pure name norm (for reference)
    }


def best_match(query, catalog):
    """
    Match algorithm from match2.py:
    - SequenceMatcher ratio (text similarity)
    - Word overlap / Jaccard-like score
    - Combined score = 0.5 * seq + 0.5 * overlap
    Applied against the enriched 'norm' field of each catalog entry.
    """
    norm_q = normalize(query)
    q_words = set(norm_q.split())

    scored = []
    for entry in catalog:
        norm_e = entry['norm']
        e_words = set(norm_e.split())

        seq = difflib.SequenceMatcher(None, norm_q, norm_e).ratio()

        if q_words and e_words:
            overlap = len(q_words & e_words) / max(len(q_words), len(e_words))
        else:
            overlap = 0.0

        combined = 0.5 * seq + 0.5 * overlap
        scored.append((combined, seq, overlap, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    combined, seq, overlap, best_entry = scored[0]

    return best_entry, round(combined, 3)


def detect_description_column(rows):
    """Auto-detect which column contains product descriptions."""
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
    # Fallback: column with longest average value
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

        # Load catalog from Supabase
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

        catalog = [build_catalog_entry(row) for row in catalog_raw]

        # Match each client product
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

        # Build JSON response in Make-compatible format
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
