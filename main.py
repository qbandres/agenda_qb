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
        password=os.getenv('POSTGRES_PASSWORD'),
        port=os.getenv('POSTGRES_PORT', '5432')
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

# --- NUEVO: OBTENER CATEGORÍAS DEL USUARIO DESDE LA DB ---
async def get_user_categories(username):
    query = f"SELECT categoria, subcategoria FROM categorias_agenda WHERE username = '{username}' AND estado = 'ACTIVO'"
    try:
        results = await execute_sql(query)
        if not results:
            return "No tienes categorías configuradas."
        
        cat_map = {}
        for r in results:
            cat = r.get('categoria', '')
            sub = r.get('subcategoria', '')
            if cat not in cat_map:
                cat_map[cat] = []
            cat_map[cat].append(sub)
            
        prompt_text = ""
        for cat, subs in cat_map.items():
            prompt_text += f"\n   - '{cat}': [{', '.join(subs)}]"
        return prompt_text
    except Exception as e:
        logger.error(f"Error cargando categorías: {e}")
        return ""

# --- CEREBRO IA (MEJORADO PARA TABLA DINÁMICA) ---
def get_system_prompt(user_id, username, categorias_dinamicas):
    return f"""
Actúas como "Jarvis", un Asistente Personal Ejecutivo para @{username}.
Gestionas la tabla `agenda_personal` en PostgreSQL.

### 1. JERARQUÍA DE CLASIFICACIÓN (ESTRICTA):
Basado en la base de datos, estas son las únicas categorías y subcategorías (proyectos) válidas para este usuario:
{categorias_dinamicas}

⚠️ REGLA CRÍTICA PARA ASIGNACIÓN:
- Tienes PROHIBIDO inventar subcategorías. Debes buscar la que mejor encaje de la lista de arriba.
- Si el usuario menciona algo que NO encaja claramente en ninguna de las subcategorías de la lista, debes clasificar la `category` como "LIBRE" y la `subcategory` como "LIBRE".
- Cuando asignes "LIBRE", utiliza el campo `user_reply` para comunicarle al usuario que no encontraste un proyecto coincidente y PREGÚNTALE si está de acuerdo en guardarlo como LIBRE o prefiere crear una categoría nueva.

NIVEL 3: TIPO (Campo `tipo_entrada`)
   - 'TAREA', 'RECORDATORIO', 'NOTA', 'CULTURA', 'GASTO'.

### 2. ESTADO (Campo `estado`)
   - Solo usar: 'Open' o 'Closed'.

### 3. REGLAS SQL PARA BÚSQUEDAS (CRÍTICO):
- **BÚSQUEDA PROFUNDA:** Cuando el usuario busque un tema, busca coincidencias en `categoria`, `subcategoria` Y `resumen` usando `OR`.
- **PRIVACIDAD:** SIEMPRE incluye `AND telegram_user_id = {user_id}`.
- **ORDEN:** Siempre `ORDER BY categoria ASC, fecha_evento ASC`.

### FORMATO JSON:
{{
  "intent": "SAVE" | "QUERY" | "DELETE" | "UPDATE",
  "reasoning": "...",
  "sql_query": "SELECT ...",
  "save_data": {{
      "category": "Una categoría de la lista o LIBRE",
      "subcategory": "La subcategoría exacta de la lista o LIBRE",
      "entry_type": "TAREA...",
      "summary": "...",
      "full_content": "...",
      "event_date": "YYYY-MM-DD HH:MM:SS" (or null),
      "extra_data": {{}},
      "status": "Open"
  }},
  "user_reply": "Mensaje normal, O pregunta si usaste la categoría LIBRE."
}}
"""

async def process_with_ai(content_type, content_data, current_date, user_id, username, categorias_dinamicas):
    sys_instruction = get_system_prompt(user_id, username, categorias_dinamicas)
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user = update.effective_user.first_name
    await update.message.reply_text(f"👋 **Hola {user}!**\nSoy Jarvis v2. Gestiono Tareas, Eventos y Recordatorios.")

async def master_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # --- NUEVO: Extraemos la lista de proyectos en tiempo real para este usuario ---
    categorias_dinamicas = await get_user_categories(username)
    
    text_input = update.message.text or ""

    if text_input.lower().startswith(("/start", "reiniciar")):
        await start(update, context)
        return

    if context.user_data.get('state') == 'WAITING_EDIT':
        original_data = context.user_data.get('pending_save')
        await update.message.reply_text("🔄 Procesando corrección...")
        correction_prompt = f"DATOS ORIGINALES: {json.dumps(original_data)}\nCORRECCIÓN SOLICITADA: '{text_input}'\nGenera el nuevo JSON de guardado."
        context.user_data['state'] = None
        ai_response = await process_with_ai('text', correction_prompt, current_date, user_id, username, categorias_dinamicas)
        if ai_response and ai_response.get('intent') == 'SAVE':
             await show_save_confirmation(update, context, ai_response)
        else:
             await update.message.reply_text("❌ No se pudo procesar la corrección.")
        return

    ai_response = None
    if text_input:
        ai_response = await process_with_ai('text', text_input, current_date, user_id, username, categorias_dinamicas)
    elif update.message.photo:
        await update.message.reply_text("👁️ Analizando imagen...")
        photo_file = await update.message.photo[-1].get_file()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp:
            await photo_file.download_to_drive(temp.name)
            ai_response = await process_with_ai('image', temp.name, current_date, user_id, username, categorias_dinamicas)
            os.remove(temp.name)
    elif update.message.voice:
        await update.message.reply_text("🎧 Procesando audio...")
        voice_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp:
            await voice_file.download_to_drive(temp.name)
            ai_response = await process_with_ai('audio', temp.name, current_date, user_id, username, categorias_dinamicas)
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
            msg = "📑 **Resultados de Búsqueda**\n"
            current_cat = None
            
            for r in results:
                cat = r.get('categoria', 'GENERAL').upper()
                sub = r.get('subcategoria', 'General')
                
                if cat != current_cat:
                    msg += f"\n📂 **{cat}**\n" + ("─" * 20) + "\n"
                    current_cat = cat
                
                rid = r.get('id')
                tipo = r.get('tipo_entrada', 'NOTA')
                resumen = escape_markdown(r.get('resumen', ''))
                date_val = r.get('fecha_evento')
                date_str = f"📅 {date_val.strftime('%d/%m %H:%M')}" if date_val else ""
                
                icon = {'TAREA': '📝', 'RECORDATORIO': '⏰', 'CULTURA': '🎭', 'GASTO': '💰'}.get(tipo, '🔹')
                msg += f"{icon} `ID {rid}` | *{sub}*\n   └ {resumen} {date_str}\n\n"

            await update.message.reply_text(msg, parse_mode='Markdown')

    elif intent in ['DELETE', 'UPDATE']:
        sql = ai_response.get('sql_query')
        context.user_data['pending_sql'] = sql
        
        preview_msg = ""
        try:
            if "WHERE" in sql.upper():
                where_clause = sql[sql.upper().index("WHERE"):]
                preview_sql = f"SELECT * FROM agenda_personal {where_clause}"
                if not preview_sql.strip().upper().startswith("SELECT"):
                    preview_sql = ""
                
                if preview_sql:
                    affected_rows = await execute_sql(preview_sql)
                    if affected_rows:
                        preview_msg = "\n\n⚠️ **Ítems Afectados:**\n"
                        for r in affected_rows:
                            rid = r.get('id')
                            sub = r.get('subcategoria', 'General')
                            resumen = escape_markdown(r.get('resumen', ''))
                            preview_msg += f"• `ID {rid}`: *{sub}* - {resumen}\n"
                    else:
                        preview_msg = "\n\n⚠️ **Atención:** No se encontraron ítems que coincidan (0 afectados)."
        except Exception as e:
            logger.error(f"Error preview: {e}")

        await update.message.reply_text(
            f"⚠️ **Confirmación de Acción**\n\n¿Desea ejecutar la siguiente operación?\n`{sql}`{preview_msg}", 
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
    
    # Extraemos el mensaje de la IA para mostrarlo junto con la confirmación
    ai_msg = data.get('user_reply', '')
    if ai_msg:
        await update.message.reply_text(f"🤖 Jarvis: {ai_msg}")
    
    resumen = escape_markdown(info.get('summary') or "Sin resumen")
    categoria = escape_markdown(info.get('category') or "GENERAL")
    subcategoria = escape_markdown(info.get('subcategory') or "General")
    tipo = escape_markdown(info.get('entry_type') or "NOTA")
    fecha = escape_markdown(str(info.get('event_date') or "Indefinida"))

    msg = (
        f"📋 **Confirmar Registro**\n\n"
        f"📂 **{categoria}** › _{subcategoria}_\n"
        f"🏷️ **Tipo:** {tipo}\n"
        f"📝 **Nota:** {resumen}\n"
        f"📅 **Fecha:** {fecha}"
    )

    keyboard = [[
        InlineKeyboardButton("✅ Confirmar", callback_data="save"),
        InlineKeyboardButton("✏️ Editar", callback_data="edit")
    ], [InlineKeyboardButton("❌ Descartar", callback_data="cancel")]]

    try:
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Markdown Error: {e}")
        await update.message.reply_text(msg.replace("*", "").replace("`", "").replace("_", ""), reply_markup=InlineKeyboardMarkup(keyboard))

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
            await query.edit_message_text(f"✅ Guardado (ID: {new_id})")
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