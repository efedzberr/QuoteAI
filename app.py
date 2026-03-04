import os
import re
import difflib
import json
from flask import Flask, request, jsonify
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
    """Detecta automáticamente cuál columna contiene las descripciones de productos."""
    if not rows:
        return None
    
    # Columnas candidatas por nombre
    priority_names = ['descripcion', 'description', 'producto', 'product', 
                      'item', 'articulo', 'article', 'nombre', 'name', 
                      'desc', 'material', 'concepto']
    
    first_row = rows[0]
    columns = list(first_row.keys())
    
    # Buscar por nombre prioritario
    for col in columns:
        if any(p in col.lower() for p in priority_names):
            return col
    
    # Si no, buscar la columna de texto más larga
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
            return jsonify({"error": "No se recibieron datos"}), 400

        # Los productos del cliente pueden venir como "rows", "productos", "items", etc.
        client_products = (
            data.get('rows') or
            data.get('productos') or
            data.get('items') or
            data.get('articulos') or
            data.get('products') or
            []
        )

        if not client_products:
            return jsonify({"error": "No se encontraron productos en el request"}), 400

        # Detectar columna de descripción automáticamente
        desc_column = detect_description_column(client_products)
        if not desc_column:
            return jsonify({"error": "No se pudo detectar la columna de descripción"}), 400

        # Cargar catálogo completo desde Supabase
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        response = supabase.table("Products").select("CodigoArt, DescCortaArt, Precio").execute()
        catalog_raw = response.data

        if not catalog_raw:
            return jsonify({"error": "El catálogo está vacío"}), 400

        # Preparar catálogo normalizado
        catalog = []
        for row in catalog_raw:
            catalog.append({
                'code': row.get('CodigoArt', ''),
                'name': row.get('DescCortaArt', ''),
                'norm': normalize(row.get('DescCortaArt', '')),
                'price': str(row.get('Precio', ''))
            })

        # Hacer matching para cada producto
        resultados = []
        for item in client_products:
            descripcion = str(item.get(desc_column, '') or '').strip()
            if not descripcion:
                continue

            match, score = best_match(descripcion, catalog)
            resultados.append({
                "descripcion_original": descripcion,
                "columna_detectada": desc_column,
                "codigo": match['code'],
                "nombre_catalogo": match['name'],
                "precio": match['price'],
                "confianza": round(score, 3),
                "requiere_revision": score < 0.7
            })

        return jsonify({
            "resultados": resultados,
            "total": len(resultados),
            "requieren_revision": sum(1 for r in resultados if r['requiere_revision']),
            "columna_detectada": desc_column
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
