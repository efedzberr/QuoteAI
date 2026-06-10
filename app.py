import os
import re
import time
import difflib
import json
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, Response
from supabase import create_client

# ===========================================================================
# FASE 1 — Matching acelerado
# ---------------------------------------------------------------------------
# Cambios vs. la versión anterior (sin tocar el contrato con Bolt/Make):
#   1. El catálogo se carga UNA sola vez en memoria (no en cada request) con
#      un cliente de Supabase reutilizado y un TTL configurable.
#   2. Se construye un ÍNDICE INVERTIDO (palabra -> productos) al cargar.
#   3. El fuzzy y el match por keywords ya NO recorren los ~30k productos:
#      corren solo sobre ~100 candidatos pre-filtrados por el índice invertido.
#   4. Se elimina el lookup por-línea a Supabase de la Pasada 0 (el catálogo
#      completo ya está en memoria).
#   5. Endpoint POST /catalog/refresh para invalidar la caché tras actualizar
#      el catálogo. La caché también se precalienta al arrancar.
# ===========================================================================

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


# ===========================================================================
# CATÁLOGO EN MEMORIA (caché) + ÍNDICE INVERTIDO  —  núcleo de la Fase 1
# ===========================================================================
# El catálogo (~30k productos) se carga y normaliza UNA sola vez por proceso.
# Bajo gunicorn cada worker tiene su propia copia (son unos pocos MB), así que
# tras actualizar el catálogo hay que llamar /catalog/refresh o reiniciar.
# ---------------------------------------------------------------------------
_CACHE = {
    "catalog":   None,   # lista de entries (build_catalog_entry)
    "by_code":   {},     # norm_code -> entry         (lookup O(1))
    "by_norm":   {},     # norm_desc -> entry         (match exacto O(1))
    "inverted":  {},     # keyword   -> set(índices)  (pre-filtro de candidatos)
    "loaded_at": 0,
}
# TTL de la caché en segundos. Pon None para que NUNCA expire por tiempo
# (se refresca solo a mano vía /catalog/refresh).
_CACHE_TTL = 6 * 3600

_supabase = None


def _get_supabase():
    """Crea el cliente de Supabase una sola vez y lo reutiliza."""
    global _supabase
    if _supabase is None:
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase


def _fetch_all_products():
    """
    Trae TODO el catálogo paginando. IMPORTANTE: PostgREST (la capa que usa
    Supabase por debajo) limita las respuestas a 1000 filas por defecto y
    supabase-py no pagina automáticamente, así que lo hacemos con .range().
    Solo pedimos las columnas que build_catalog_entry realmente usa.
    """
    PAGE_SIZE = 1000
    rows = []
    offset = 0
    supabase = _get_supabase()
    while True:
        resp = supabase.table("products").select(
            "CodigoArt, DescCortaArt, Precio"
        ).range(offset, offset + PAGE_SIZE - 1).execute()

        page = resp.data or []
        if not page:
            break

        rows.extend(page)

        # Si la página vino con menos de PAGE_SIZE filas, es la última.
        if len(page) < PAGE_SIZE:
            break

        offset += PAGE_SIZE

        # Salvaguarda contra bucles infinitos (catálogos enormes o bugs).
        if offset > 100000:
            break

    return rows


def _build_inverted(catalog):
    """
    Índice invertido: palabra clave -> conjunto de índices de productos que la
    contienen como token. Solo tokens significativos (>=3 chars, no stopwords),
    igual que extract_keywords. Permite pre-filtrar candidatos en O(keywords)
    en vez de recorrer los 30k por línea.
    """
    inv = {}
    for i, entry in enumerate(catalog):
        for tok in entry['norm_set']:
            if len(tok) >= 3 and tok not in STOPWORDS:
                inv.setdefault(tok, set()).add(i)
    return inv


def load_catalog(force=False):
    """Carga (o recarga) el catálogo en memoria y reconstruye los índices."""
    fresh = _CACHE["catalog"] is not None and (
        _CACHE_TTL is None or (time.time() - _CACHE["loaded_at"] < _CACHE_TTL)
    )
    if fresh and not force:
        return

    raw = _fetch_all_products()
    catalog = [build_catalog_entry(row) for row in raw]

    # Índice por código normalizado (lookup O(1)).
    by_code = {e['norm_code']: e for e in catalog if e['norm_code']}

    # Índice por descripción normalizada (match exacto O(1)). Si dos productos
    # comparten la misma descripción normalizada, conservamos el primero.
    by_norm = {}
    for e in catalog:
        if e['norm'] and e['norm'] not in by_norm:
            by_norm[e['norm']] = e

    _CACHE.update({
        "catalog":   catalog,
        "by_code":   by_code,
        "by_norm":   by_norm,
        "inverted":  _build_inverted(catalog),
        "loaded_at": time.time(),
    })


def get_catalog():
    """Devuelve la caché, cargándola de forma perezosa si aún no existe."""
    if _CACHE["catalog"] is None or (
        _CACHE_TTL is not None and time.time() - _CACHE["loaded_at"] >= _CACHE_TTL
    ):
        load_catalog()
    return _CACHE


def candidates(query, top_n=100):
    """
    Pre-filtro: devuelve hasta `top_n` productos del catálogo que comparten
    palabras clave con la query, ordenados por número de coincidencias. El
    fuzzy y el match por keywords corren SOLO sobre estos candidatos.
    """
    cache = get_catalog()
    kws = extract_keywords(normalize(query))
    if not kws:
        return []

    inv = cache["inverted"]
    catalog = cache["catalog"]
    cnt = Counter()
    for kw in kws:
        for idx in inv.get(kw, ()):
            cnt[idx] += 1

    if not cnt:
        return []

    return [catalog[i] for i, _ in cnt.most_common(top_n)]


# ---------------------------------------------------------------------------
# PASADA 0 — Búsqueda directa por código (en memoria)
# ---------------------------------------------------------------------------
def code_lookup(code, by_code):
    """
    Busca un producto por su CodigoArt en el catálogo en memoria (O(1)).
    Como el catálogo completo está cacheado, ya no hace falta consultar
    Supabase por línea: si el código no está aquí, no está en el catálogo.
    Retorna el entry del producto o None si no existe.
    """
    if not code:
        return None
    norm = normalize_code(code)
    if not norm:
        return None
    return by_code.get(norm)


# ---------------------------------------------------------------------------
# PASADA 2 — Fuzzy Match (SequenceMatcher + Jaccard) sobre candidatos
# ---------------------------------------------------------------------------
def fuzzy_match(query, entries):
    """Igual que antes, pero recibe el subconjunto de candidatos (~100) en
    lugar de todo el catálogo."""
    if not entries:
        return None, 0.0

    norm_q  = normalize(query)
    q_words = set(norm_q.split())

    scored = []
    for entry in entries:
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
# PASADA 3 — Intersección de palabras clave sobre candidatos
# ---------------------------------------------------------------------------
def keyword_ilike_match(query, entries):
    """
    Intersección de palabras clave hecha en memoria sobre los candidatos ya
    pre-filtrados (sin acentos). Para cada palabra clave significativa marca
    los productos cuyo nombre normalizado la contengan como substring, cuenta
    coincidencias y elige al de mayor número (desempate por fuzzy score).

    Devuelve: (mejor_entry, keywords_coincidentes, total_keywords)
    """
    norm_q   = normalize(query)
    keywords = extract_keywords(norm_q)

    if not keywords or not entries:
        return None, 0, 0

    code_counter = Counter()
    for kw in keywords:
        for entry in entries:
            # 'norm' ya está sin acentos y en mayúsculas, así que el substring
            # match es totalmente insensible a acentos y a may/min.
            if kw in entry['norm']:
                code_counter[entry['code']] += 1

    if not code_counter:
        return None, 0, 0

    # Indexar los candidatos por código para resolver el ganador
    cand_by_code = {e['code']: e for e in entries if e['code']}

    total_keywords = len(keywords)
    max_count = code_counter.most_common(1)[0][1]
    winners = [c for c, count in code_counter.items() if count == max_count]

    if len(winners) == 1:
        best_code = winners[0]
    else:
        best_code = max(
            winners,
            key=lambda c: difflib.SequenceMatcher(
                None, norm_q, cand_by_code[c]['norm']
            ).ratio()
        )

    best_entry = cand_by_code.get(best_code)
    if best_entry is None:
        return None, 0, 0

    return {
        'code':  best_entry['code'],
        'name':  best_entry['name'],
        'price': best_entry['price'],
        'norm':  best_entry['norm'],
    }, max_count, total_keywords


# ---------------------------------------------------------------------------
# Lógica principal de matching (tres pasadas)
# ---------------------------------------------------------------------------
def match_product(query, code):
    """
    Pasada 0: Búsqueda por código (si se proporcionó).  -> confianza 1.0
    Pasada 1: Match exacto por descripción normalizada. -> confianza 1.0
    Pasada 2: Fuzzy sobre candidatos. score >= 0.70     -> directo
    Pasada 3: Intersección de keywords sobre candidatos -> 0.40-0.69
    Si nada matchea -> NO ENCONTRADO.
    """
    cache    = get_catalog()
    by_code  = cache["by_code"]
    by_norm  = cache["by_norm"]

    # --- Pasada 0: Código del producto ---
    code_to_try = code
    if not code_to_try and query:
        # Si no hay código explícito, intentar extraerlo del texto
        code_to_try = extract_code_from_text(query)

    if code_to_try:
        match0 = code_lookup(code_to_try, by_code)
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
    norm_query = normalize(query)
    if norm_query and norm_query in by_norm:
        match_exact = by_norm[norm_query]
        return {
            "codigo":            match_exact['code'],
            "nombre_catalogo":   match_exact['name'],
            "precio":            match_exact['price'],
            "confianza":         1.0,
            "requiere_revision": False,
            "metodo":            "exacto",
        }

    # --- Pre-filtro: candidatos del índice invertido (para Pasadas 2 y 3) ---
    # NOTA: el fuzzy ya solo corre sobre estos candidatos. Si una descripción
    # no comparte ninguna palabra clave con el catálogo, no habrá candidatos y
    # se marca NO ENCONTRADO (antes el fuzzy barría los 30k; en la práctica
    # esos matches puro-carácter eran raros y casi siempre espurios). Si hiciera
    # falta, sube `top_n` en candidates().
    cand = candidates(query)
    if not cand:
        return {
            "codigo":            "NO ENCONTRADO",
            "nombre_catalogo":   "NO ENCONTRADO",
            "precio":            "",
            "confianza":         0.0,
            "requiere_revision": True,
            "metodo":            "ninguno",
        }

    # --- Pasada 2: Fuzzy ---
    match1, score1 = fuzzy_match(query, cand)
    if match1 and score1 >= 0.70:
        return {
            "codigo":            match1['code'],
            "nombre_catalogo":   match1['name'],
            "precio":            match1['price'],
            "confianza":         score1,
            "requiere_revision": False,
            "metodo":            "fuzzy",
        }

    # --- Pasada 3: Intersección de keywords sobre candidatos ---
    match2, matched_kw, total_kw = keyword_ilike_match(query, cand)

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


# ===========================================================================
# FASE 2 — Validación semántica con LLM (deltas-only)
# ---------------------------------------------------------------------------
# En vez de pedirle al LLM que regenere TODO el JSON (lento, se trunca o se pasa
# del timeout del webhook), aquí:
#   - Solo se envían las líneas con match PROPUESTO a validar (metodo
#     fuzzy/keyword). Las de codigo/exacto son certeras y NO se tocan; las de
#     "ninguno" ya son NO ENCONTRADO.
#   - El LLM devuelve SOLO los deltas (idx, confianza, requiere_revision, y
#     opcionalmente NO ENCONTRADO). El merge se hace aquí, en memoria.
#   - Las líneas ambiguas se procesan en lotes y en paralelo acotado.
# Requiere la variable de entorno ANTHROPIC_API_KEY y el paquete `anthropic`.
# Si no hay API key, la evaluación se omite (degrada a solo matching).
# Esto REEMPLAZA al módulo de Anthropic que vivía en Make.
# ===========================================================================
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
LLM_BATCH_SIZE    = int(os.environ.get("LLM_BATCH_SIZE", "40"))
LLM_MAX_WORKERS   = int(os.environ.get("LLM_MAX_WORKERS", "4"))

_anthropic_client = None


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic  # import perezoso: solo si se va a usar
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


EVALUATOR_SYSTEM = (
    "Eres un evaluador semantico de productos industriales en Mexico. Recibiras "
    "un arreglo JSON de lineas, cada una con: idx, descripcion_original, "
    "nombre_catalogo y metodo. Valida si nombre_catalogo corresponde "
    "semanticamente a descripcion_original y devuelve SOLO las correcciones.\n\n"
    "CRITERIOS DE CONFIANZA:\n"
    "- Mismo producto escrito distinto (espacios, mayusculas, orden) -> 1.0\n"
    "- Mismo producto con abreviaturas conocidas o typos menores -> 0.90-0.99\n"
    "- Probablemente el mismo pero con duda razonable -> 0.70-0.89\n"
    "- Relacionado pero con diferencias importantes (modelo, capacidad, voltaje) -> 0.50-0.69\n"
    "- No relacionados / completamente diferentes -> 0.0-0.49\n\n"
    "ABREVIACIONES (no penalices por estas): P/=Para, C/=Con, S/=Sin, Jgo=Juego, "
    "Mca=Marca, No.=Numero, Pza/Pz=Pieza, Term=Termomagnetico, Interr=Interruptor, "
    "Volt/V=Voltaje, Amp/A=Amperes, Polos/P=Polos.\n\n"
    "REGLA NO ENCONTRADO: si nombre_catalogo ya dice \"NO ENCONTRADO\", o si el "
    "producto no tiene NINGUNA relacion con la descripcion, pon en esa linea "
    "codigo=\"NO ENCONTRADO\", nombre_catalogo=\"NO ENCONTRADO\", confianza=0.0.\n\n"
    "REGLA DE REVISION: requiere_revision = true si y solo si confianza < 0.90.\n\n"
    "SALIDA (CRITICO): responde UNICAMENTE con un arreglo JSON valido, sin texto "
    "ni markdown, empezando con '[' y terminando con ']'. Para cada linea de "
    "entrada incluye exactamente: {\"idx\": <n>, \"confianza\": <0.0-1.0>, "
    "\"requiere_revision\": <bool>}. Incluye ademas \"codigo\" y \"nombre_catalogo\" "
    "SOLO si la cambias a \"NO ENCONTRADO\". No agregues otros campos ni lineas "
    "que no esten en la entrada."
)


def _parse_deltas(text):
    """Extrae el arreglo JSON de la respuesta del modelo de forma robusta."""
    s = (text or "").strip()
    a, b = s.find("["), s.rfind("]")
    if a == -1 or b == -1 or b < a:
        return []
    try:
        return json.loads(s[a:b + 1])
    except Exception:
        return []


def _eval_batch(batch):
    """Evalúa un lote de líneas ambiguas y devuelve la lista de deltas."""
    payload = [
        {
            "idx":                  r["_idx"],
            "descripcion_original": r["descripcion_original"],
            "nombre_catalogo":      r["nombre_catalogo"],
            "metodo":               r["metodo"],
        }
        for r in batch
    ]
    try:
        client = _get_anthropic()
        msg = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,
            system=EVALUATOR_SYSTEM,
            messages=[{"role": "user",
                       "content": json.dumps(payload, ensure_ascii=False)}],
        )
        text = "".join(
            getattr(b, "text", "") for b in msg.content
            if getattr(b, "type", "") == "text"
        )
        return _parse_deltas(text)
    except Exception:
        # Si un lote falla, no tumbamos el job: esas líneas quedan como vinieron.
        return []


def evaluate_with_llm(resultados):
    """Aplica la validación semántica del LLM SOLO a las líneas fuzzy/keyword.
    Devuelve la misma lista con confianza/requiere_revision corregidos (y, en su
    caso, marcados como NO ENCONTRADO). El formato de salida no cambia."""
    if not ANTHROPIC_API_KEY:
        return resultados

    ambiguous = []
    for i, r in enumerate(resultados):
        if r.get("metodo") in ("fuzzy", "keyword"):
            r["_idx"] = i
            ambiguous.append(r)

    if not ambiguous:
        return resultados

    batches = [ambiguous[k:k + LLM_BATCH_SIZE]
               for k in range(0, len(ambiguous), LLM_BATCH_SIZE)]

    deltas = []
    workers = max(1, min(LLM_MAX_WORKERS, len(batches)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(_eval_batch, batches):
            deltas.extend(res or [])

    for d in deltas:
        if not isinstance(d, dict):
            continue
        i = d.get("idx")
        if not isinstance(i, int) or not (0 <= i < len(resultados)):
            continue
        r = resultados[i]
        if d.get("codigo") == "NO ENCONTRADO" or d.get("nombre_catalogo") == "NO ENCONTRADO":
            r["codigo"]            = "NO ENCONTRADO"
            r["nombre_catalogo"]   = "NO ENCONTRADO"
            r["precio"]            = ""
            r["confianza"]         = 0.0
            r["requiere_revision"] = True
            continue
        if "confianza" in d:
            try:
                conf = round(float(d["confianza"]), 3)
            except (TypeError, ValueError):
                continue
            r["confianza"]         = conf
            r["requiere_revision"] = bool(conf < 0.90)

    for r in resultados:
        r.pop("_idx", None)

    return resultados


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

        # Catálogo desde la caché en memoria (se carga una sola vez por proceso).
        cache = get_catalog()
        if not cache["catalog"]:
            return Response(
                json.dumps({"error": "El catalogo esta vacio"}, ensure_ascii=False),
                status=400, mimetype='application/json'
            )

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

            result = match_product(descripcion, codigo_in)

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

        # FASE 2 — Validación semántica con LLM (solo líneas fuzzy/keyword,
        # salida deltas-only, merge en memoria). Sustituye al módulo de Anthropic
        # que vivía en Make.
        resultados = evaluate_with_llm(resultados)

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


@app.route('/catalog/refresh', methods=['POST'])
def catalog_refresh():
    """Invalida y recarga la caché del catálogo. Llamar tras actualizar el
    catálogo en Supabase (o reiniciar el servicio)."""
    try:
        load_catalog(force=True)
        return Response(
            json.dumps({"status": "ok", "productos": len(_CACHE["catalog"] or [])}),
            status=200, mimetype='application/json'
        )
    except Exception as e:
        return Response(
            json.dumps({"error": str(e)}, ensure_ascii=False),
            status=500, mimetype='application/json'
        )


@app.route('/health', methods=['GET'])
def health():
    return Response(
        json.dumps({
            "status": "ok",
            "catalogo_cargado": _CACHE["catalog"] is not None,
            "productos": len(_CACHE["catalog"] or []),
        }),
        status=200, mimetype='application/json'
    )


# Precalentar la caché al arrancar. Bajo gunicorn esto corre una vez por worker
# al importar el módulo; con `python app.py` corre antes de levantar el server.
try:
    load_catalog()
except Exception as _e:
    print(f"[startup] no se pudo precargar el catálogo: {_e}")


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
