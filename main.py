import os
from dotenv import load_dotenv
load_dotenv()

from config import logger
from db import init_db
from handlers import start, master_handler, button_callback
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters

if __name__ == '__main__':
    init_db()
    app = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.VOICE) & (~filters.COMMAND), master_handler))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("🚀 JARVIS PROFESSIONAL SYSTEM RUNNING...")
    app.run_polling()
