import os
import logging
import json
import tempfile
import base64
from datetime import datetime
import psycopg2
from psycopg2.extras import Json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from openai import AsyncOpenAI
from dotenv import load_dotenv
from PIL import Image

# --- CONFIGURACIÓN ---
load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- BASE DE DATOS ---
def get_db_connection():
    return psycopg2.connect(
        host=os.getenv('POSTGRES_HOST'),
        database=os.getenv('POSTGRES_DB'),
        user=os.getenv('POSTGRES_USER'),
        password=os.getenv('POSTGRES_PASSWORD')
    )

def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Tabla Principal
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agenda_personal (
                id SERIAL PRIMARY KEY,
                telegram_user_id BIGINT,
                username VARCHAR(100),
                fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                categoria VARCHAR(50),
                subcategoria VARCHAR(100),
                tipo_entrada VARCHAR(50),  -- AQUÍ ESTÁ LA CLAVE
                resumen TEXT,
                contenido_completo TEXT,
                fecha_evento TIMESTAMP,
                datos_extra JSONB,
                estado VARCHAR(20) DEFAULT 'PENDIENTE'
            );
        """)
        
        # Asegurar columna username
        cur.execute("""
            DO $$ 
            BEGIN 
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agenda_personal' AND column_name='username') THEN 
                    ALTER TABLE agenda_personal ADD COLUMN username VARCHAR(100); 
                END IF;
                
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agenda_personal' AND column_name='tipo_entrada') THEN 
                    ALTER TABLE agenda_personal ADD COLUMN tipo_entrada VARCHAR(50); 
                END IF;
            END $$;
        """)
        
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error DB init: {e}")

# --- UTILIDADES ---
def clean_and_parse_json(text_response):
    cleaned = text_response.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except:
        return None

def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

# --- CEREBRO IA (PROMPT MEJORADO) ---
def get_system_prompt(user_id, username):
    return f"""
Actúas como "Jarvis", un Asistente Personal Ejecutivo para @{username}.
Gestionas la tabla `agenda_personal` en PostgreSQL.

### 1. CLASIFICACIÓN DE ENTRADAS (CRÍTICO):
Debes asignar el campo `tipo_entrada` automáticamente:
- **'TAREA'**: Algo que requiere acción/esfuerzo (ej: "Comprar leche", "Llamar cliente", "Estudiar").
- **'EVENTO'**: Una cita con hora fija (ej: "Reunión 3pm", "Dentista", "Vuelo").
- **'MEMO'**: Información pasiva, recordatorios sin acción, listas de deseos (ej: "Cumpleaños de mamá", "Ver película X", "Libro recomendado", "Clave Wifi").

### 2. REGLAS SQL:
- **TABLA ÚNICA:** `agenda_personal`.
- **PRIVACIDAD:** SIEMPRE `WHERE telegram_user_id = {user_id}`.
- **FILTROS INTELIGENTES:**
  - Si piden "tareas" o "pendientes" -> `AND tipo_entrada = 'TAREA'`.
  - Si piden "cumpleaños" -> `AND resumen ILIKE '%cumpleaños%'`.
  - Si piden "agenda" -> Muestra TODO (Eventos, Tareas y Memos relevantes).
  - Si piden "libros/pelis" -> `AND categoria = 'MEDIA_BACKLOG'`.

### 3. CATEGORÍAS:
WORK, STUDY, PERSONAL, MEDIA_BACKLOG (Pelis/Libros), QUICK_NOTE.

### FORMATO JSON:
{{
  "intent": "SAVE" | "QUERY" | "DELETE" | "UPDATE",
  "reasoning": "Explica por qué elegiste el tipo_entrada",
  "sql_query": "SELECT id, tipo_entrada, categoria, resumen, fecha_evento FROM agenda_personal WHERE telegram_user_id = {user_id} ...",
  "save_data": {{
      "category": "WORK" | "STUDY" | "PERSONAL" | "MEDIA_BACKLOG" | "QUICK_NOTE",
      "entry_type": "TAREA" | "EVENTO" | "MEMO",
      "subcategory": "...",
      "summary": "...",
      "full_content": "...",
      "event_date": "YYYY-MM-DD HH:MM:SS" (or null),
      "extra_data": {{}}
  }},
  "user_reply": "Respuesta al usuario"
}}
"""

async def process_with_ai(content_type, content_data, current_date, user_id, username):
    sys_instruction = get_system_prompt(user_id, username)
    messages = [{"role": "system", "content": f"{sys_instruction}\n\nFecha Actual: {current_date}"}]

    if content_type == 'audio':
        try:
            with open(content_data, "rb") as audio_file:
                transcription = await client.audio.transcriptions.create(model="whisper-1", file=audio_file)
            messages.append({"role": "user", "content": f"Audio: {transcription.text}"})
        except Exception:
            return None
    elif content_type == 'image':
        try:
            base64_image = encode_image(content_data)
            messages.append({
                "role": "user", 
                "content": [
                    {"type": "text", "text": "Analiza esta imagen para la agenda."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            })
        except Exception:
            return None
    elif content_type == 'text':
        messages.append({"role": "user", "content": content_data})

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.0
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Error GPT: {e}")
        return None

# --- MANEJADORES ---

async def execute_sql(query):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        logger.info(f"SQL Exec: {query}")
        cur.execute(query)
        if cur.description:
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
            result = [dict(zip(cols, row)) for row in rows]
        else:
            conn.commit()
            result = cur.rowcount
        conn.close()
        return result
    except Exception as e:
        logger.error(f"SQL Error: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user = update.effective_user.first_name
    await update.message.reply_text(f"👋 **Hola {user}!**\nSoy Jarvis v2. Ahora distingo entre Tareas, Eventos y Recordatorios.")

async def master_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    current_date = datetime.now().strftime("%Y-02-10 %H:%M:%S")
    
    text_input = update.message.text or ""

    if text_input.lower().startswith(("/start", "reiniciar")):
        await start(update, context)
        return

    if context.user_data.get('state') == 'WAITING_EDIT':
        original_data = context.user_data.get('pending_save')
        await update.message.reply_text("🔄 Procesando corrección...")
        correction_prompt = f"DATOS: {json.dumps(original_data)}\nCORRECCIÓN: '{text_input}'\nMantén intent='SAVE'."
        context.user_data['state'] = None
        ai_response = await process_with_ai('text', correction_prompt, current_date, user_id, username)
        
        if ai_response and ai_response.get('intent') == 'SAVE':
             await show_save_confirmation(update, context, ai_response)
        else:
             await update.message.reply_text("❌ No entendí la corrección.")
        return

    ai_response = None
    if text_input:
        await update.message.reply_text("⚡ Pensando...")
        ai_response = await process_with_ai('text', text_input, current_date, user_id, username)
    elif update.message.photo:
        await update.message.reply_text("👁️ Analizando...")
        photo_file = await update.message.photo[-1].get_file()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp:
            await photo_file.download_to_drive(temp.name)
            ai_response = await process_with_ai('image', temp.name, current_date, user_id, username)
            os.remove(temp.name)
    elif update.message.voice:
        await update.message.reply_text("🎧 Escuchando...")
        voice_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp:
            await voice_file.download_to_drive(temp.name)
            ai_response = await process_with_ai('audio', temp.name, current_date, user_id, username)
            os.remove(temp.name)

    if not ai_response:
        await update.message.reply_text("😵 Error de IA.")
        return

    intent = ai_response.get('intent')
    
    if intent == 'SAVE':
        await show_save_confirmation(update, context, ai_response)
        
    elif intent == 'QUERY':
        sql = ai_response.get('sql_query')
        results = await execute_sql(sql)
        
        if not results:
            await update.message.reply_text(f"📭 Nada encontrado. (Tabla: `agenda_personal`)")
        else:
            msg = "🔍 **Resultados:**\n\n"
            for r in results:
                rid = r.get('id', '-')
                # Mostramos un icono según el TIPO DE ENTRADA
                tipo = r.get('tipo_entrada', 'OTRO')
                icon = "📝" if tipo == 'TAREA' else "📅" if tipo == 'EVENTO' else "🧠"
                
                cat = r.get('categoria', 'N/A')
                summ = r.get('resumen', 'Sin título')
                date = r.get('fecha_evento')
                date_str = date.strftime('%d/%m %H:%M') if date else ""
                
                msg += f"🆔 {rid} | {icon} {tipo} | {cat}\n📌 {summ}\n📅 {date_str}\n\n"
            await update.message.reply_text(msg)

    elif intent in ['DELETE', 'UPDATE']:
        sql = ai_response.get('sql_query')
        context.user_data['pending_sql'] = sql
        await update.message.reply_text(
            f"⚠️ Confirmar SQL:\n`{sql}`", 
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Ejecutar", callback_data="exec_sql"), InlineKeyboardButton("❌ Cancelar", callback_data="cancel")]])
        )
    else:
        await update.message.reply_text(ai_response.get('user_reply', "Entendido."))

async def show_save_confirmation(update, context, data):
    info = data['save_data']
    context.user_data['pending_save'] = info
    
    # Iconos visuales para el tipo
    tipo_map = {'TAREA': '🛠️ TAREA (Hacer)', 'EVENTO': '📅 EVENTO (Asistir)', 'MEMO': '🧠 MEMO (Recordar)'}
    tipo_str = tipo_map.get(info.get('entry_type'), info.get('entry_type'))

    msg = (
        f"📝 **Confirmar:**\n\n"
        f"🏷️ Tipo: {tipo_str}\n" # Mostramos el tipo
        f"📂 Categoría: {info.get('category')}\n"
        f"📌 Resumen: {info.get('summary')}\n"
        f"📅 Fecha: {info.get('event_date') or ''}\n"
    )
    keyboard = [[InlineKeyboardButton("✅ Guardar", callback_data="save"), InlineKeyboardButton("✏️ Corregir", callback_data="edit")], [InlineKeyboardButton("❌ Descartar", callback_data="cancel")]]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    if data == "save":
        item = context.user_data.get('pending_save')
        if item:
            conn = get_db_connection()
            cur = conn.cursor()
            # GUARDAMOS EL TIPO_ENTRADA (entry_type)
            cur.execute("""
                INSERT INTO agenda_personal 
                (telegram_user_id, username, categoria, subcategoria, tipo_entrada, fecha_creacion, resumen, contenido_completo, fecha_evento, datos_extra, estado)
                VALUES (%s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, 'APPROVED') 
                RETURNING id
            """, (user_id, username, item['category'], item.get('subcategory'), item.get('entry_type', 'MEMO'), item['summary'], item.get('full_content'), item.get('event_date'), Json(item.get('extra_data'))))
            new_id = cur.fetchone()[0]
            conn.commit()
            conn.close()
            await query.edit_message_text(f"✅ Guardado (ID: {new_id})")
            context.user_data.pop('pending_save', None)
    elif data == "edit":
        context.user_data['state'] = 'WAITING_EDIT'
        await query.edit_message_text("✏️ Escribe el cambio...")
    elif data == "cancel":
        await query.edit_message_text("❌ Cancelado.")
        context.user_data.clear()
    elif data == "exec_sql":
        sql = context.user_data.get('pending_sql')
        if sql:
            res = await execute_sql(sql)
            await query.edit_message_text(f"✅ Hecho. ({res})")
        else:
            await query.edit_message_text("❌ Error SQL.")

if __name__ == '__main__':
    init_db()
    app = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) | filters.PHOTO | filters.VOICE, master_handler))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("🔥 JARVIS V2 (Tipos de Entrada) RUNNING...")
    app.run_polling()