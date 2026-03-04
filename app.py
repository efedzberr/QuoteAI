import os
import re
import difflib
import json
from flask import Flask, request, Response
from supabase import create_client

app = Flask(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

def normalize(text):
    if not text:
        return ""
    text = str(text).upper().strip()
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text

def best_match(query, catalog):
    norm_q = normalize(query)
    q_words = set(norm_q.split())
    results = []

    for c in catalog:
        seq = difflib.SequenceMatcher(None, norm_q, c['norm']).ratio()
        c_words = set(c['norm'].split())
        overlap = len(q_words & c_words) / max(len(q_words), len(c_words)) if q_words and c_words else 0
        combined = 0.5 * seq + 0.5 * overlap
        results.append((combined, seq, overlap, c))

    results.sort(key=lambda x: x[0], reverse=True)
    combined, seq, overlap, cat = results[0]
    return cat, combined

def detect_description_column(rows):
    if not rows:
        return None
    
    priority_names = ['descripcion', 'description', 'producto', 'product', 
                      'item', 'articulo', 'article', 'nombre', 'name', 
                      'desc', 'material', 'concepto']
    
    first_row = rows[0]
    columns = list(first_row.keys())
    
    for col in columns:
        if any(p in col.lower() for p in priority_names):
            return col
    
    best_col = None
    best_avg_len = 0
    for col in columns:
        values = [str(r.get(col, '') or '') for r in rows[:10]]
        avg_len = sum(len(v) for v in values) / max(len(values), 1)
        if avg_len > best_avg_len:
            best_avg_len = avg_len
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
            data.get('rows') or
            data.get('productos') or
            data.get('items') or
            data.get('articulos') or
            data.get('products') or
            []
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

        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        response = supabase.table("products").select("CodigoArt, DescCortaArt, Precio").execute()
        catalog_raw = response.data

        if not catalog_raw:
            return Response(
                json.dumps({"error": "El catalogo esta vacio"}, ensure_ascii=False),
                status=400, mimetype='application/json'
            )

        catalog = []
        for row in catalog_raw:
            catalog.append({
                'code': row.get('CodigoArt', ''),
                'name': row.get('DescCortaArt', ''),
                'norm': normalize(row.get('DescCortaArt', '')),
                'price': str(row.get('Precio', ''))
            })

        resultados = []
        for item in client_products:
            descripcion = str(item.get(desc_column, '') or '').strip()
            if not descripcion:
                continue

            match, score = best_match(descripcion, catalog)
            resultados.append({
                "original_text": descripcion,
                "descripcion_original": descripcion,
                "codigo": match['code'],
                "nombre_catalogo": match['name'],
                "precio": match['price'],
                "confianza": round(score, 3),
                "requiere_revision": score < 0.7
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

@app.route('/health', methods=['GET'])
def health():
    return Response(
        json.dumps({"status": "ok"}),
        status=200,
        mimetype='application/json'
    )

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
