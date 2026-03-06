import os
import json
import tempfile
from datetime import datetime

from psycopg2.extras import Json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import logger
from db import get_db_connection, execute_sql, get_user_categories, register_user, is_user_registered, get_upcoming_reminders, mark_reminder_sent
from ai import process_with_ai
from utils import escape_markdown


REMINDER_INTERVALS = [
    (60, "⏰ *Recordatorio en 1 HORA*"),
    (5, "⚠️ *Recordatorio en 5 MINUTOS*"),
    (1, "🚨 *Recordatorio en 1 MINUTO*"),
]


async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Job periódico que revisa eventos próximos y envía alertas."""
    for minutes, alert_title in REMINDER_INTERVALS:
        label = f"{minutes}m"
        events = get_upcoming_reminders(minutes)
        for event in events:
            fecha = event['fecha_evento'].strftime('%d/%m/%Y %H:%M')
            msg = (
                f"{alert_title}\n\n"
                f"📂 {event.get('categoria', 'GENERAL')} › {event.get('subcategoria', 'General')}\n"
                f"📝 {event.get('resumen', 'Sin detalle')}\n"
                f"📅 {fecha}"
            )
            try:
                await context.bot.send_message(
                    chat_id=event['telegram_user_id'],
                    text=msg,
                    parse_mode='Markdown'
                )
                mark_reminder_sent(event['id'], label)
                logger.info(f"Recordatorio {label} enviado - ID:{event['id']} User:{event['telegram_user_id']}")
            except Exception as e:
                logger.error(f"Error enviando recordatorio: {e}")


async def send_long_message(update, text, chunk_size=4000):
    """Envía un mensaje partiéndolo en chunks si supera el límite de Telegram (4096 chars)."""
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        try:
            await update.message.reply_text(chunk, parse_mode='Markdown')
        except Exception:
            await update.message.reply_text(chunk.replace("*", "").replace("`", "").replace("_", ""))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user = update.effective_user
    telegram_user_id = user.id
    username = user.username or user.first_name
    nombre = user.full_name

    is_new = register_user(telegram_user_id, username, nombre)

    if is_new:
        await update.message.reply_text(
            f"👋 **Bienvenido {user.first_name}!**\n\n"
            f"Te has registrado exitosamente.\n"
            f"Ya tienes categorías configuradas: *Trabajo, Entretenimiento, Personal y Recordatorio*.\n\n"
            f"Envíame texto, fotos o audios y los organizaré por ti.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            f"👋 **Hola {user.first_name}!**\nSoy Jarvis v2. Gestiono Tareas, Eventos y Recordatorios.",
            parse_mode='Markdown'
        )


async def master_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not is_user_registered(user_id):
        await update.message.reply_text("⚠️ No estás registrado. Usa /start para comenzar.")
        return

    categorias_dinamicas = await get_user_categories(user_id)

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
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
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
        sql = ai_response.get('sql_query', '').strip().rstrip('.')
        logger.info(f"QUERY SQL generado: {sql}")
        results = await execute_sql(sql)
        if results is None:
            await update.message.reply_text("❌ Error al ejecutar la consulta. Revisa los logs.")
        elif not results:
            await update.message.reply_text("ℹ️ No se encontraron resultados para esa búsqueda.")
        else:
            keys = list(results[0].keys())

            if keys == ['categoria']:
                msg = "📂 **Mis Categorías**\n" + ("─" * 20) + "\n"
                for r in results:
                    msg += f"• {r['categoria']}\n"

            elif keys == ['subcategoria']:
                msg = "📋 **Subcategorías / Proyectos**\n" + ("─" * 20) + "\n"
                for i, r in enumerate(results, 1):
                    msg += f"{i}. {r['subcategoria']}\n"

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

            await send_long_message(update, msg)

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
