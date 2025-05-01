from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from datetime import datetime
import logging
import time
import subprocess
import sys
from telegram.error import TimedOut, NetworkError
import psycopg2
from contextlib import contextmanager
from dotenv import load_dotenv
import os

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

# Константы
DEFAULT_RATE = 190
PING_INTERVAL = 300  # 5 минут
LIB_UPDATE_INTERVAL = 86400  # 1 день
WAITING_FINE_AMOUNT, WAITING_NEW_RATE, CONFIRM_RESET = range(3)

class Database:
    @contextmanager
    def get_connection(self):
        """Контекстный менеджер для подключения к Supabase"""
        conn = psycopg2.connect(os.getenv('DATABASE_URL'))
        try:
            yield conn
        finally:
            conn.close()

    def init_db(self):
        """Инициализация таблицы (уже сделана в Supabase)"""
        pass

class BotData:
    def __init__(self):
        self.db = Database()
        
    def get_user_data(self, user_id: int) -> dict:
        """Получение данных пользователя из Supabase"""
        with self.db.get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            data = cur.fetchone()
            if data:
                return {
                    'total_hours': data[1],
                    'fine': data[2],
                    'rate': data[3],
                    'last_update': data[4]
                }
            return {
                'total_hours': 0,
                'fine': 0,
                'rate': DEFAULT_RATE,
                'last_update': datetime.now().strftime('%Y-%m-%d')
            }

    def update_user_data(self, user_id: int, data: dict):
        """Обновление данных пользователя в Supabase"""
        with self.db.get_connection() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, total_hours, fine, rate, last_update)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    total_hours = EXCLUDED.total_hours,
                    fine = EXCLUDED.fine,
                    rate = EXCLUDED.rate,
                    last_update = EXCLUDED.last_update
            """, (
                user_id,
                data.get('total_hours', 0),
                data.get('fine', 0),
                data.get('rate', DEFAULT_RATE),
                data.get('last_update', datetime.now().strftime('%Y-%m-%d'))
            ))
            conn.commit()

bot_data = BotData()

def get_keyboard():
    return ReplyKeyboardMarkup([
        ["📊 Статистика"],
        ["💰 Аванс", "💵 Зарплата"],
        ["➕ Добавить штраф", "✏️ Изменить ставку"],
        ["🔄 Сброс статистики"]
    ], resize_keyboard=True)

def calculate_work_hours(start_time: str, end_time: str) -> float:
    try:
        start_h, start_m = map(int, start_time.split(':'))
        end_h, end_m = map(int, end_time.split(':'))
        
        if end_h < start_h or (end_h == start_h and end_m < start_m):
            end_h += 24
            
        total_minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
        return round(total_minutes / 60, 2)
    except Exception as e:
        logger.error(f"Ошибка расчета времени: {e}")
        return 0.0

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_data = bot_data.get_user_data(user_id)
    
    await update.message.reply_text(
        "🕒 Введите рабочее время в формате ЧЧ:ММ-ЧЧ:ММ",
        reply_markup=get_keyboard()
    )
    return ConversationHandler.END

async def handle_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text in ["✅ Да, сбросить", "❌ Нет, отменить"]:
        return ConversationHandler.END
        
    user_id = update.message.from_user.id
    user_data = bot_data.get_user_data(user_id)
    
    try:
        start_time, end_time = update.message.text.split('-')
        hours = calculate_work_hours(start_time, end_time)
        
        if hours <= 0:
            raise ValueError("Некорректное время работы")
        
        user_data['total_hours'] += hours
        user_data['last_update'] = datetime.now().strftime('%Y-%m-%d')
        bot_data.update_user_data(user_id, user_data)
        
        await update.message.reply_text(
            f"✅ Добавлено: {start_time}-{end_time}\n"
            f"🕒 Отработано: {hours:.2f} ч.\n"
            f"📊 Всего за месяц: {user_data['total_hours']:.2f} ч.",
            reply_markup=get_keyboard()
        )
    except Exception as e:
        logger.error(f"Ошибка обработки времени: {e}")
        await update.message.reply_text(
            "❌ Неверный формат времени. Пример: 09:30-18:45 или 22:00-02:30",
            reply_markup=get_keyboard()
        )
    return ConversationHandler.END

async def request_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚠️ Вы уверены, что хотите сбросить статистику?",
        reply_markup=ReplyKeyboardMarkup(
            [["✅ Да, сбросить", "❌ Нет, отменить"]],
            resize_keyboard=True
        )
    )
    return CONFIRM_RESET

async def confirm_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.message.text == "✅ Да, сбросить":
            user_id = update.message.from_user.id
            bot_data.update_user_data(user_id, {
                'total_hours': 0,
                'fine': 0,
                'rate': DEFAULT_RATE,
                'last_update': datetime.now().strftime('%Y-%m-%d')
            })
            await update.message.reply_text(
                "🔄 Статистика сброшена!",
                reply_markup=get_keyboard()
            )
        else:
            await update.message.reply_text(
                "Сброс отменён",
                reply_markup=get_keyboard()
            )
    except Exception as e:
        logger.error(f"Ошибка подтверждения сброса: {e}")
    finally:
        return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Действие отменено",
        reply_markup=get_keyboard()
    )
    return ConversationHandler.END

async def send_ping(context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.get_me()
        logger.debug("Ping успешно отправлен")
        return True
    except Exception as e:
        logger.warning(f"Ошибка ping: {e}")
        return False

async def check_lib_updates():
    try:
        logger.info("Проверка обновлений библиотек...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "python-telegram-bot"])
        return True
    except Exception as e:
        logger.error(f"Ошибка при обновлении библиотек: {e}")
        return False

async def maintain_connection(context: ContextTypes.DEFAULT_TYPE):
    await send_ping(context)
    await check_lib_updates()

async def init_jobs(application: Application):
    try:
        application.job_queue.run_repeating(
            maintain_connection,
            interval=PING_INTERVAL,
            first=10
        )
        logger.info("Фоновые задачи инициализированы")
    except Exception as e:
        logger.error(f"Ошибка инициализации задач: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    logger.error(msg="Ошибка в боте:", exc_info=error)
    
    if isinstance(error, (psycopg2.OperationalError, psycopg2.InterfaceError)):
        logger.error("Ошибка подключения к Supabase")
        if update and hasattr(update, 'message'):
            await update.message.reply_text(
                "⚠️ Ошибка подключения к базе данных. Попробуйте позже.",
                reply_markup=get_keyboard()
            )
    elif isinstance(error, (TimedOut, NetworkError)):
        logger.warning("Проблемы с соединением Telegram")
    elif update and hasattr(update, 'message'):
        try:
            await update.message.reply_text(
                "⚠️ Временная ошибка. Попробуйте еще раз.",
                reply_markup=get_keyboard()
            )
        except:
            pass

def main():
    TOKEN = os.getenv('7821553363:AAGCEbiQ29WkKe-XYyr_EL4eWZcwf-AWxGI')
    
    # Первоначальное обновление библиотек
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "python-telegram-bot"])
    except Exception as e:
        logger.error(f"Ошибка при обновлении: {e}")

    while True:
        try:
            app = Application.builder().token(TOKEN).build()
            
            # Инициализация фоновых задач
            app.post_init = init_jobs
            
            # Обработчики команд
            app.add_handler(CommandHandler("start", start))
            
            # Обработчик сброса статистики
            reset_handler = ConversationHandler(
                entry_points=[MessageHandler(filters.Regex("^🔄 Сброс статистики$"), request_reset)],
                states={
                    CONFIRM_RESET: [MessageHandler(filters.Regex("^(✅ Да, сбросить|❌ Нет, отменить)$"), confirm_reset)],
                },
                fallbacks=[CommandHandler("cancel", cancel)],
            )
            app.add_handler(reset_handler)
            
            # Обработчик времени работы
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time))
            
            # Обработчик ошибок
            app.add_error_handler(error_handler)
            
            logger.info("Бот запускается...")
            app.run_polling(
                poll_interval=5.0,
                drop_pending_updates=True
            )
        except Exception as e:
            logger.error(f"Критическая ошибка: {e}. Перезапуск через 30 секунд...")
            time.sleep(30)

if __name__ == '__main__':
    main()