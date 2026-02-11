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
                tipo_entrada VARCHAR(50),
                resumen TEXT,
                contenido_completo TEXT,
                fecha_evento TIMESTAMP,
                datos_extra JSONB,
                estado VARCHAR(20) DEFAULT 'APPROVED'
            );
        """)
        
        # Asegurar columnas (Mantenemos tu lógica de migración DO $$)
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

def escape_markdown(text):
    """Escapa caracteres especiales para evitar errores de Telegram BadRequest"""
    if not text: return ""
    parse_chars = ['_', '*', '`', '[']
    for char in parse_chars:
        text = text.replace(char, f"\\{char}")
    return text

def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

# --- CEREBRO IA (MEJORADO PARA BÚSQUEDAS) ---
def get_system_prompt(user_id, username):
    return f"""
Actúas como "Jarvis", un Asistente Personal Ejecutivo para @{username}.
Gestionas la tabla `agenda_personal` en PostgreSQL.

### 1. JERARQUÍA DE CLASIFICACIÓN:
NIVEL 1: CONTEXTO (Campo `categoria`) 
   - 'TRABAJO': Construcción, ingeniería, SOW, clientes.
   - 'PERSONAL': Familia, hogar, salud, gastos.
   - 'ACADEMICO': Cursos, Data Science, Python, estudio.
   - 'ENTRETENIMIENTO': Música, canciones, obras, libros, películas.

NIVEL 2: TIPO (Campo `tipo_entrada`)
   - 'TAREA': Requiere acción (Hacer).
   - 'RECORDATORIO': Evento con fecha (Asistir).
   - 'NOTA': Dato pasivo (Recordar).
   - 'CULTURA': SOLO para Entretenimiento (Ver/Leer/Escuchar).
   - 'GASTO': Salida de dinero.

### 2. REGLAS SQL (CRÍTICO PARA BÚSQUEDAS):
- Si el usuario pide ver "tareas de trabajo", la consulta DEBE ser: `SELECT ... FROM agenda_personal WHERE telegram_user_id = {user_id} AND categoria = 'TRABAJO' AND tipo_entrada = 'TAREA'`.
- Usa siempre `ILIKE '%termino%'` para búsquedas en `resumen`.
- PRIVACIDAD: SIEMPRE `WHERE telegram_user_id = {user_id}`.

### FORMATO JSON:
{{
  "intent": "SAVE" | "QUERY" | "DELETE" | "UPDATE",
  "reasoning": "...",
  "sql_query": "...",
  "save_data": {{
      "category": "TRABAJO" | "PERSONAL" | "ACADEMICO" | "ENTRETENIMIENTO",
      "entry_type": "TAREA" | "RECORDATORIO" | "NOTA" | "CULTURA" | "GASTO",
      "subcategory": "...",
      "summary": "...",
      "full_content": "...",
      "event_date": "YYYY-MM-DD HH:MM:SS" (or null),
      "extra_data": {{}}
  }},
  "user_reply": "..."
}}
"""

async def process_with_ai(content_type, content_data, current_date, user_id, username):
    sys_instruction = get_system_prompt(user_id, username)
    messages = [{"role": "system", "content": f"{sys_instruction}\n\nFecha Actual: {current_date}"}]

    if content_type == 'audio':
        try:
            with open(content_data, "rb") as audio_file:
                transcription = await client.audio.transcriptions.create(model="whisper-1", file=audio_file)
            messages.append({"role": "user", "content": f"Audio recibido: {transcription.text}"})
        except Exception as e:
            logger.error(f"Error Whisper: {e}")
            return None
    elif content_type == 'image':
        try:
            base64_image = encode_image(content_data)
            messages.append({
                "role": "user", 
                "content": [
                    {"type": "text", "text": "Analiza esta imagen y extrae la información relevante para la agenda según las categorías establecidas."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            })
        except Exception as e:
            logger.error(f"Error Vision: {e}")
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
    await update.message.reply_text(f"💼 **Jarvis Executive v2**\nSistema en línea para @{user}. ¿En qué puedo asistirle hoy?")

async def master_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    text_input = update.message.text or ""

    if text_input.lower().startswith(("/start", "reiniciar")):
        await start(update, context)
        return

    if context.user_data.get('state') == 'WAITING_EDIT':
        original_data = context.user_data.get('pending_save')
        await update.message.reply_text("🔄 Procesando corrección...")
        correction_prompt = f"DATOS ORIGINALES: {json.dumps(original_data)}\nCORRECCIÓN SOLICITADA: '{text_input}'\nGenera el nuevo JSON de guardado."
        context.user_data['state'] = None
        ai_response = await process_with_ai('text', correction_prompt, current_date, user_id, username)
        if ai_response and ai_response.get('intent') == 'SAVE':
             await show_save_confirmation(update, context, ai_response)
        else:
             await update.message.reply_text("❌ No se pudo procesar la corrección.")
        return

    ai_response = None
    if text_input:
        ai_response = await process_with_ai('text', text_input, current_date, user_id, username)
    elif update.message.photo:
        await update.message.reply_text("👁️ Analizando imagen...")
        photo_file = await update.message.photo[-1].get_file()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp:
            await photo_file.download_to_drive(temp.name)
            ai_response = await process_with_ai('image', temp.name, current_date, user_id, username)
            os.remove(temp.name)
    elif update.message.voice:
        await update.message.reply_text("🎧 Procesando audio...")
        voice_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp:
            await voice_file.download_to_drive(temp.name)
            ai_response = await process_with_ai('audio', temp.name, current_date, user_id, username)
            os.remove(temp.name)

    if not ai_response:
        await update.message.reply_text("😵 Lo siento, hubo un error procesando la solicitud.")
        return

    intent = ai_response.get('intent')
    
    if intent == 'SAVE':
        await show_save_confirmation(update, context, ai_response)
    elif intent == 'QUERY':
        sql = ai_response.get('sql_query')
        results = await execute_sql(sql)
        if not results:
            await update.message.reply_text("ℹ️ No se encontraron registros que coincidan con su búsqueda.")
        else:
            msg = "📑 **Registros Encontrados**\n" + ("─" * 15) + "\n"
            for r in results:
                # Lógica visual profesional
                date_val = r.get('fecha_evento')
                date_str = date_val.strftime('%d/%m %H:%M') if date_val else "Sin fecha"
                msg += f"• `ID {r['id']}` | **{r['categoria']}**\n  {r['resumen']} ({date_str})\n\n"
            await update.message.reply_text(msg, parse_mode='Markdown')
    elif intent in ['DELETE', 'UPDATE']:
        sql = ai_response.get('sql_query')
        context.user_data['pending_sql'] = sql
        await update.message.reply_text(
            f"⚠️ **Confirmación de Acción**\n\n¿Desea ejecutar la siguiente operación?\n`{sql}`", 
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Ejecutar", callback_data="exec_sql"), InlineKeyboardButton("Cancelar", callback_data="cancel")]]),
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(ai_response.get('user_reply', "Entendido."))

async def show_save_confirmation(update, context, data):
    info_raw = data.get('save_data')
    if not info_raw:
        await update.message.reply_text("❌ Error: No se detectaron datos para guardar.")
        return

    info = info_raw[0] if isinstance(info_raw, list) and len(info_raw) > 0 else info_raw
    context.user_data['pending_save'] = info
    
    # Formato visual profesional
    resumen = escape_markdown(info.get('summary') or "Sin resumen")
    categoria = escape_markdown(info.get('category') or "GENERAL")
    tipo = escape_markdown(info.get('entry_type') or "NOTA")
    fecha = escape_markdown(str(info.get('event_date') or "Indefinida"))

    msg = (
        f"📋 **Propuesta de Registro**\n\n"
        f"**Ámbito:** `{categoria}`\n"
        f"**Tipo:** {tipo}\n"
        f"**Detalle:** {resumen}\n"
        f"**Fecha:** {fecha}\n\n"
        f"¿Desea confirmar el guardado?"
    )

    keyboard = [[
        InlineKeyboardButton("✅ Confirmar", callback_data="save"),
        InlineKeyboardButton("✏️ Editar", callback_data="edit")
    ], [InlineKeyboardButton("❌ Descartar", callback_data="cancel")]]

    try:
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Markdown Error: {e}")
        await update.message.reply_text(msg.replace("*", "").replace("`", ""), reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    if query.data == "save":
        item = context.user_data.get('pending_save')
        if item:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO agenda_personal 
                (telegram_user_id, username, categoria, subcategoria, tipo_entrada, fecha_creacion, resumen, contenido_completo, fecha_evento, datos_extra, estado)
                VALUES (%s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, 'APPROVED') 
                RETURNING id
            """, (user_id, username, item.get('category'), item.get('subcategory'), item.get('entry_type'), 
                  item['summary'], item.get('full_content'), item.get('event_date'), Json(item.get('extra_data'))))
            new_id = cur.fetchone()[0]
            conn.commit()
            conn.close()
            await query.edit_message_text(f"✅ Registro guardado exitosamente. (ID: {new_id})")
            context.user_data.pop('pending_save', None)
    elif query.data == "edit":
        context.user_data['state'] = 'WAITING_EDIT'
        await query.edit_message_text("✍️ Por favor, escriba los cambios o la nueva información:")
    elif query.data == "cancel":
        await query.edit_message_text("❌ Operación cancelada.")
        context.user_data.clear()
    elif query.data == "exec_sql":
        sql = context.user_data.get('pending_sql')
        if sql:
            res = await execute_sql(sql)
            await query.edit_message_text(f"✅ Acción completada con éxito. ({res} filas afectadas)")

if __name__ == '__main__':
    init_db()
    app = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.VOICE) & (~filters.COMMAND), master_handler))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("🚀 JARVIS PROFESSIONAL SYSTEM RUNNING...")
    app.run_polling()