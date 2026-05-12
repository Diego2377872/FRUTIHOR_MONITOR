import ee
import json
import requests
import math
import sqlite3
import os
import tempfile
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from datetime import datetime, timedelta

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

# ---------- Inicialización de Earth Engine (robusta con cuenta de servicio) ----------
ee_initialized = False
ee_error_msg = None

try:
    json_creds = os.environ.get("EE_CREDENTIALS")
    if json_creds:
        # Cargar el JSON como diccionario (debe estar minificado en una línea)
        creds = json.loads(json_creds)
        private_key = creds["private_key"]
        client_email = creds["client_email"]
        # Usar key_data en lugar de archivo
        credentials = ee.ServiceAccountCredentials(client_email, key_data=private_key)
        ee.Initialize(credentials)
        print("✅ Conectado a Earth Engine con cuenta de servicio (key_data)")
        ee_initialized = True
    else:
        # Modo desarrollo local (sin variables)
        ee.Initialize()
        print("✅ Conectado a Earth Engine en modo local (desarrollo)")
        ee_initialized = True
except Exception as e:
    ee_error_msg = str(e)
    print(f"❌ Error en Earth Engine: {e}")
    print("⚠️ Las funciones de índices satelitales y mapa de heladas no estarán disponibles.")
    ee_initialized = False

# Ruta al GeoJSON
PATH_GEOJSON = os.path.join(os.path.dirname(__file__), 'parcelas_.geojson')

# ---------- Base de datos SQLite ----------
DB_PATH = os.path.join(os.path.dirname(__file__), 'cuaderno_campo.db')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS labores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parcela_id TEXT NOT NULL,
            fecha TEXT NOT NULL,
            tipo_labor TEXT NOT NULL,
            insumo TEXT,
            dosis TEXT,
            operario TEXT,
            observaciones TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ---------- Servir frontend ----------
@app.route('/')
def servir_frontend():
    return send_from_directory('static', 'index.html')

# ---------- Endpoint: parcelas ----------
@app.route('/api/parcelas')
def obtener_parcelas():
    try:
        with open(PATH_GEOJSON, encoding='utf-8') as f:
            data = json.load(f)
        for feature in data['features']:
            if 'id' not in feature:
                props = feature.get('properties', {})
                feature['id'] = str(props.get('id', feature.get('id', '')))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------- Endpoint: diagnóstico de Earth Engine ----------
@app.route('/api/ee_status')
def ee_status():
    return jsonify({
        "ee_initialized": ee_initialized,
        "creds_present": bool(os.environ.get("EE_CREDENTIALS")),
        "error_message": ee_error_msg if not ee_initialized else None
    })

# ---------- Endpoint: historial climático ----------
@app.route('/api/historial_clima', methods=['POST'])
def obtener_historial_clima():
    try:
        req = request.json
        feature = req['feature']
        geom = feature['geometry']
        coords = None
        if geom['type'] == 'Polygon':
            coords = geom['coordinates'][0]
        elif geom['type'] == 'MultiPolygon':
            coords = geom['coordinates'][0][0]
        else:
            return jsonify({"error": "Geometría no soportada"}), 400
        if not coords or len(coords) < 3:
            return jsonify({"error": "Coordenadas inválidas"}), 400
        lats = [p[1] for p in coords]
        lons = [p[0] for p in coords]
        lat_centro = sum(lats) / len(lats)
        lon_centro = sum(lons) / len(lons)
        end_date = datetime.now() - timedelta(days=1)
        start_date = end_date - timedelta(days=29)
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat_centro,
            "longitude": lon_centro,
            "daily": ["temperature_2m_max", "temperature_2m_min", "relative_humidity_2m_mean", "precipitation_sum"],
            "timezone": "America/Asuncion",
            "past_days": 30,
            "forecast_days": 0
        }
        response = requests.get(url, params=params)
        data = None
        if response.status_code == 200:
            data = response.json()
        if not data or "daily" not in data or "time" not in data["daily"]:
            url_archive = "https://archive-api.open-meteo.com/v1/archive"
            params_archive = {
                "latitude": lat_centro,
                "longitude": lon_centro,
                "start_date": start_date.strftime('%Y-%m-%d'),
                "end_date": end_date.strftime('%Y-%m-%d'),
                "daily": ["temperature_2m_max", "temperature_2m_min", "relative_humidity_2m_mean", "precipitation_sum"],
                "timezone": "America/Asuncion"
            }
            resp_archive = requests.get(url_archive, params=params_archive)
            if resp_archive.status_code == 200:
                data = resp_archive.json()
            else:
                return jsonify({"error": "No se pudo obtener clima histórico"}), 500
        if "daily" not in data or "time" not in data["daily"]:
            return jsonify({"error": "Formato de respuesta inválido"}), 500
        fechas_totales = data["daily"]["time"]
        temp_max_total = data["daily"]["temperature_2m_max"]
        temp_min_total = data["daily"]["temperature_2m_min"]
        humedad_total = data["daily"]["relative_humidity_2m_mean"]
        precip_total = data["daily"].get("precipitation_sum", [0]*len(fechas_totales))
        fechas_filtradas = []
        temp_max_filt = []
        temp_min_filt = []
        humedad_filt = []
        precip_filt = []
        fecha_inicio_dt = start_date
        for i, fecha_str in enumerate(fechas_totales):
            fecha_dt = datetime.strptime(fecha_str, '%Y-%m-%d')
            if fecha_dt >= fecha_inicio_dt and fecha_dt <= end_date:
                fechas_filtradas.append(fecha_str)
                temp_max_filt.append(temp_max_total[i])
                temp_min_filt.append(temp_min_total[i])
                humedad_filt.append(humedad_total[i])
                precip_filt.append(precip_total[i] if i < len(precip_total) else 0)
        return jsonify({
            "fechas": fechas_filtradas,
            "temp_min": temp_min_filt,
            "temp_max": temp_max_filt,
            "humedad": humedad_filt,
            "precipitacion": precip_filt
        })
    except Exception as e:
        print("Error en /api/historial_clima:", str(e))
        return jsonify({"error": str(e)}), 500

# ---------- Endpoint: análisis NDVI/IC/NDWI (con mejor manejo de errores) ----------
@app.route('/api/analizar', methods=['POST'])
def analizar():
    if not ee_initialized:
        return jsonify({"error": f"Earth Engine no disponible. Motivo: {ee_error_msg or 'No inicializado'}"}), 503
    try:
        req = request.json
        feature = req['feature']
        tipo = req['tipo']
        fecha_usuario = req.get('fecha', datetime.now().strftime('%Y-%m-%d'))
        fecha_target = datetime.strptime(fecha_usuario, '%Y-%m-%d')
        fecha_inicio = (fecha_target - timedelta(days=15)).strftime('%Y-%m-%d')
        fecha_fin = (fecha_target + timedelta(days=5)).strftime('%Y-%m-%d')
        
        # Extraer coordenadas
        geom = feature['geometry']
        if geom['type'] == 'Polygon':
            coords = geom['coordinates'][0]
        elif geom['type'] == 'MultiPolygon':
            coords = geom['coordinates'][0][0]
        else:
            return jsonify({"error": "Geometría no soportada"}), 400

        roi = ee.Geometry.Polygon(coords)
        imagen = (ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
                  .filterBounds(roi)
                  .filterDate(fecha_inicio, fecha_fin)
                  .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 30))
                  .sort('CLOUDY_PIXEL_PERCENTAGE')
                  .first())
        
        # Verificar si existe imagen
        img_info = imagen.getInfo()
        if not img_info:
            return jsonify({"error": "No hay imágenes disponibles para la fecha seleccionada (nubes o falta de cobertura)"}), 404
        
        if tipo == 'NDVI':
            indice = imagen.normalizedDifference(['B8', 'B4']).rename('NDVI').clip(roi)
            viz = {'min': 0.1, 'max': 0.9, 'palette': ['#ff1744', '#ffff00', '#00e676', '#1b5e20']}
            umbrales = [0.4, 0.7]
            etiquetas = ["Vigor Bajo (<0.4)", "Vigor Medio (0.4-0.7)", "Vigor Alto (>0.7)"]
            colores = ["#e74c3c", "#f1c40f", "#2ecc71"]
        elif tipo == 'IC':
            indice = imagen.select('B8').divide(imagen.select('B5')).subtract(1).rename('IC').clip(roi)
            viz = {'min': 0, 'max': 4, 'palette': ['#dce775', '#8bc34a', '#33691e', '#1b5e20']}
            umbrales = [1.0, 2.0]
            etiquetas = ["Clorofila Baja", "Clorofila Media", "Clorofila Alta"]
            colores = ["#dce775", "#8bc34a", "#1b5e20"]
        else:  # NDWI
            indice = imagen.normalizedDifference(['B3', 'B8']).rename('NDWI').clip(roi)
            viz = {'min': -0.4, 'max': 0.4, 'palette': ['#ffffff', '#00b0ff', '#002f6c']}
            umbrales = [0.1, 0.4]
            etiquetas = ["Suelo Seco", "Suelo Saturado", "Agua Profunda"]
            colores = ["#ffffff", "#81d4fa", "#01579b"]
        
        m1 = indice.lt(umbrales[0])
        m2 = indice.gte(umbrales[0]).And(indice.lt(umbrales[1]))
        m3 = indice.gte(umbrales[1])
        area_m1 = indice.updateMask(m1).reduceRegion(ee.Reducer.count(), roi, 10).getInfo().get(tipo, 0) * 100 / 10000
        area_m2 = indice.updateMask(m2).reduceRegion(ee.Reducer.count(), roi, 10).getInfo().get(tipo, 0) * 100 / 10000
        area_m3 = indice.updateMask(m3).reduceRegion(ee.Reducer.count(), roi, 10).getInfo().get(tipo, 0) * 100 / 10000
        areas = [area_m1, area_m2, area_m3]
        desglose = [{"etiqueta": etiquetas[i], "ha": round(areas[i], 2), "color": colores[i]} for i in range(3)]
        map_id = indice.getMapId(viz)
        stats_mean = indice.reduceRegion(reducer=ee.Reducer.mean(), geometry=roi, scale=10).getInfo()
        return jsonify({
            "valor": round(stats_mean.get(tipo, 0), 3),
            "tile_url": map_id['tile_fetcher'].url_format,
            "fecha_satelite": imagen.date().format('YYYY-MM-dd').getInfo(),
            "tipo": tipo,
            "desglose": desglose
        })
    except Exception as e:
        print("Error en /api/analizar:", str(e))
        return jsonify({"error": f"Error interno: {str(e)}"}), 500

# ---------- Mapa de heladas ----------
@app.route('/api/mapa_heladas', methods=['POST'])
def mapa_heladas():
    if not ee_initialized:
        return jsonify({"error": "Earth Engine no disponible"}), 503
    try:
        req = request.json
        feature = req['feature']
        geom = feature['geometry']
        if geom['type'] == 'Polygon':
            coords = geom['coordinates'][0]
        elif geom['type'] == 'MultiPolygon':
            coords = geom['coordinates'][0][0]
        else:
            return jsonify({"error": "Geometría no soportada"}), 400
        roi = ee.Geometry.Polygon(coords)
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30)
        collection = ee.ImageCollection('ECMWF/ERA5_LAND/DAILY_AGGR') \
            .filterDate(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')) \
            .select('temperature_2m_min') \
            .sort('system:time_start', False)
        size = collection.size().getInfo()
        if size == 0:
            return jsonify({"error": "No hay datos de temperatura en últimos 30 días"}), 404
        imagen = collection.first()
        fecha_imagen = imagen.date().format('YYYY-MM-dd').getInfo()
        temp_c = imagen.subtract(273.15).clip(roi)
        viz = {
            'min': -5,
            'max': 5,
            'palette': ['#a50f15', '#d73027', '#f46d43', '#fdae61', '#fee090', '#ffffbf', '#e0f3f8']
        }
        map_id = temp_c.getMapId(viz)
        return jsonify({
            "tile_url": map_id['tile_fetcher'].url_format,
            "fecha": fecha_imagen,
            "unidad": "°C"
        })
    except Exception as e:
        print("❌ Error en mapa_heladas:", str(e))
        return jsonify({"error": str(e)}), 500

# ---------- Pronóstico ----------
@app.route('/api/pronostico', methods=['POST'])
def obtener_pronostico():
    try:
        req = request.json
        feature = req['feature']
        geom = feature['geometry']
        if geom['type'] == 'Polygon':
            coords = geom['coordinates'][0]
        elif geom['type'] == 'MultiPolygon':
            coords = geom['coordinates'][0][0]
        else:
            return jsonify({"error": "Geometría no soportada"}), 400
        lats = [p[1] for p in coords]
        lons = [p[0] for p in coords]
        lat_centro = sum(lats) / len(lats)
        lon_centro = sum(lons) / len(lons)
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat_centro,
            "longitude": lon_centro,
            "daily": ["temperature_2m_max", "temperature_2m_min", "precipitation_sum",
                      "wind_speed_10m_max", "relative_humidity_2m_max"],
            "timezone": "America/Asuncion",
            "forecast_days": 10
        }
        response = requests.get(url, params=params)
        if response.status_code != 200:
            return jsonify({"error": "Error en API externa"}), 500
        data = response.json()
        resultados = []
        for i in range(10):
            fecha = data["daily"]["time"][i]
            tmax = data["daily"]["temperature_2m_max"][i]
            tmin = data["daily"]["temperature_2m_min"][i]
            tmed = (tmax + tmin) / 2
            prec = data["daily"]["precipitation_sum"][i]
            viento_vel = data["daily"]["wind_speed_10m_max"][i]
            humedad = data["daily"]["relative_humidity_2m_max"][i]
            diff = max(0, tmax - tmin)
            if diff > 0:
                et0 = 0.0023 * (tmed + 17.8) * math.sqrt(diff) * 15
            else:
                et0 = 0
            balance = prec - et0
            alerta_helada = (tmin <= 3)
            resultados.append({
                "fecha": fecha,
                "precipitacion_mm": round(prec, 1),
                "temperatura_c": round(tmed, 1),
                "temp_max_c": round(tmax, 1),
                "temp_min_c": round(tmin, 1),
                "balance_mm": round(balance, 1),
                "viento_velocidad_kmh": round(viento_vel, 1),
                "humedad_relativa_max": round(humedad, 0),
                "alerta_helada": alerta_helada
            })
        return jsonify(resultados)
    except Exception as e:
        print("Error en /api/pronostico:", str(e))
        return jsonify({"error": str(e)}), 500

# ---------- Recomendación ----------
@app.route('/api/recomendacion', methods=['POST'])
def recomendacion():
    try:
        req = request.json
        cultivo = req.get('cultivo', 'tomate')
        fecha_siembra = req.get('fecha_siembra')
        variedad = req.get('variedad', '')
        pronostico = req.get('pronostico', [])
        if not fecha_siembra:
            return jsonify({"recomendacion": "⚠️ Ingrese fecha de siembra para obtener fenología."})
        dias_desde_siembra = (datetime.now() - datetime.strptime(fecha_siembra, '%Y-%m-%d')).days
        fases = {
            'tomate': {'vegetativo': (0, 30), 'floracion': (31, 55), 'fructificacion': (56, 85), 'madurez': (86, 120)},
            'cebolla': {'vegetativo': (0, 40), 'bulbificacion': (41, 80), 'madurez': (81, 120)},
            'papa': {'vegetativo': (0, 30), 'tuberizacion': (31, 60), 'madurez': (61, 100)}
        }
        fase_actual = "Desconocida"
        for nombre, rango in fases.get(cultivo, {}).items():
            if rango[0] <= dias_desde_siembra <= rango[1]:
                fase_actual = nombre
                break
        if dias_desde_siembra < 0:
            fase_actual = "Pre-siembra"
        recomendacion = f"📅 **{cultivo.capitalize()}** | Días desde siembra: {max(0,dias_desde_siembra)} | Fenología: {fase_actual}\n\n"
        if cultivo == 'tomate':
            if fase_actual == 'vegetativo':
                recomendacion += "🌱 Enfocar en riego moderado y fertilización nitrogenada. Monitorear Tuta absoluta.\n"
            elif fase_actual == 'floracion':
                recomendacion += "🌸 Asegurar polinización, evitar estrés hídrico. Aplicar boro y calcio.\n"
            elif fase_actual == 'fructificacion':
                recomendacion += "🍅 Incrementar potasio. Controlar mildiu y trips.\n"
            elif fase_actual == 'madurez':
                recomendacion += "🍅 Reducir riego para concentrar azúcares. Preparar cosecha.\n"
        elif cultivo == 'cebolla':
            if fase_actual == 'vegetativo':
                recomendacion += "🧅 Riego frecuente, nitrógeno. Control de mildiu velloso.\n"
            elif fase_actual == 'bulbificacion':
                recomendacion += "🧅 Disminuir riego gradualmente. Aplicar fósforo y potasio.\n"
            elif fase_actual == 'madurez':
                recomendacion += "🧅 Suspender riego, doblar tallos para secado.\n"
        elif cultivo == 'papa':
            if fase_actual == 'vegetativo':
                recomendacion += "🥔 Riego constante, evitar encharcamiento. Control de polilla de la papa.\n"
            elif fase_actual == 'tuberizacion':
                recomendacion += "🥔 Aporque y fertilización potásica. Vigilar tizón tardío.\n"
            elif fase_actual == 'madurez':
                recomendacion += "🥔 Suspender riego 2 semanas antes de cosecha.\n"
        if pronostico and any(d.get('alerta_helada', False) for d in pronostico):
            recomendacion += "\n❄️ **ALERTA DE HELADA** en los próximos días. Acciones: riego por aspersión antes del amanecer, cobertores, aplicación de productos anticongelantes.\n"
        if pronostico and len(pronostico) > 0:
            balance_promedio = sum(d['balance_mm'] for d in pronostico) / len(pronostico)
            if balance_promedio < -2:
                recomendacion += "\n💧 **Déficit hídrico previsto** - Programar riego suplementario.\n"
            elif balance_promedio > 5:
                recomendacion += "\n⚠️ **Exceso de lluvia** - Revisar drenaje para evitar pudriciones.\n"
        return jsonify({"recomendacion": recomendacion})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------- Chat con Groq ----------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

@app.route('/api/chat', methods=['POST'])
def chat():
    try:
        data = request.json
        mensaje_usuario = data.get('mensaje')
        contexto = data.get('contexto', '')
        if not mensaje_usuario:
            return jsonify({"error": "Mensaje vacío"}), 400
        if not GROQ_API_KEY:
            return jsonify({"error": "API Key de Groq no configurada"}), 500
        messages = [
            {"role": "system", "content": "Eres Tom, un asesor agronómico experto en cultivos frutihortícolas (tomate, cebolla, papa). Respondes de forma clara y práctica."},
            {"role": "user", "content": f"Contexto: {contexto}\nPregunta: {mensaje_usuario}"}
        ]
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {GROQ_API_KEY}"}
        payload = {"model": "llama-3.3-70b-versatile", "messages": messages, "temperature": 0.7}
        response = requests.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers, timeout=30)
        if response.status_code != 200:
            return jsonify({"error": f"Error de Groq: {response.text}"}), 500
        respuesta = response.json()["choices"][0]["message"]["content"]
        return jsonify({"respuesta": respuesta})
    except Exception as e:
        print("Error en chat:", str(e))
        return jsonify({"error": str(e)}), 500

# ---------- Cuaderno de campo (eventos agrícolas) ----------
@app.route('/api/eventos', methods=['GET'])
def obtener_eventos():
    parcela_id = request.args.get('parcela_id')
    if not parcela_id:
        return jsonify({"error": "Se requiere parcela_id"}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM labores WHERE parcela_id = ? ORDER BY fecha DESC, created_at DESC", (parcela_id,))
    rows = c.fetchall()
    conn.close()
    labores = [dict(row) for row in rows]
    return jsonify(labores)

@app.route('/api/eventos', methods=['POST'])
def guardar_evento():
    data = request.json
    required = ['parcela_id', 'fecha', 'tipo']
    for field in required:
        if field not in data:
            return jsonify({"error": f"Falta campo {field}"}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO labores (parcela_id, fecha, tipo_labor, insumo, dosis, operario, observaciones)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        data['parcela_id'], data['fecha'], data['tipo'],
        data.get('insumo', ''), data.get('dosis', ''),
        data.get('operario', ''), data.get('observaciones', '')
    ))
    conn.commit()
    nuevo_id = c.lastrowid
    conn.close()
    return jsonify({"mensaje": "Labor guardada", "id": nuevo_id}), 201

@app.route('/api/eventos/<int:id>', methods=['DELETE'])
def eliminar_evento(id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM labores WHERE id = ?", (id,))
    conn.commit()
    eliminadas = c.rowcount
    conn.close()
    if eliminadas:
        return jsonify({"mensaje": "Labor eliminada"})
    else:
        return jsonify({"error": "Registro no encontrado"}), 404

# ---------- Iniciar servidor ----------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)