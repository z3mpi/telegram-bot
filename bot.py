import os
import logging
import sys
import time
import subprocess
from datetime import datetime
from contextlib import contextmanager

import psycopg2
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters
)
from telegram.error import TimedOut, NetworkError

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

class DatabaseManager:
    """Класс для управления подключением к Supabase"""
    
    @contextmanager
    def get_connection(self):
        """Контекстный менеджер для подключения к БД"""
        conn = None
        try:
            conn = psycopg2.connect(os.getenv('DATABASE_URL'))
            yield conn
        except psycopg2.Error as e:
            logger.error(f"Ошибка БД: {e}")
            raise
        finally:
            if conn:
                conn.close()

    def ensure_table_exists(self):
        """Проверка существования таблицы"""
        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    total_hours FLOAT DEFAULT 0,
                    fine FLOAT DEFAULT 0,
                    rate INTEGER DEFAULT %s,
                    last_update DATE
                )
            """, (DEFAULT_RATE,))
            conn.commit()

class BotDataManager:
    """Класс для работы с данными бота"""
    
    def __init__(self):
        self.db = DatabaseManager()
        self.db.ensure_table_exists()
        
    def get_user_data(self, user_id: int) -> dict:
        """Получение данных пользователя"""
        with self.db.get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            if data := cur.fetchone():
                return {
                    'total_hours': data[1],
                    'fine': data[2],
                    'rate': data[3],
                    'last_update': data[4]
                }
            return self._get_default_data()

    def update_user_data(self, user_id: int, data: dict):
        """Обновление данных пользователя"""
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
                data.get('last_update', datetime.now().date())
            ))
            conn.commit()

    def _get_default_data(self) -> dict:
        """Данные по умолчанию"""
        return {
            'total_hours': 0,
            'fine': 0,
            'rate': DEFAULT_RATE,
            'last_update': datetime.now().date()
        }

# Инициализация менеджера данных
data_manager = BotDataManager()

def get_keyboard() -> ReplyKeyboardMarkup:
    """Генерация клавиатуры"""
    return ReplyKeyboardMarkup([
        ["📊 Статистика"],
        ["💰 Аванс", "💵 Зарплата"],
        ["➕ Добавить штраф", "✏️ Изменить ставку"],
        ["🔄 Сброс статистики"]
    ], resize_keyboard=True)

def calculate_work_hours(time_range: str) -> float:
    """Расчет отработанных часов"""
    try:
        start, end = time_range.split('-')
        start_h, start_m = map(int, start.split(':'))
        end_h, end_m = map(int, end.split(':'))
        
        if (end_h, end_m) < (start_h, start_m):
            end_h += 24
            
        total_minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
        return round(total_minutes / 60, 2)
    except ValueError as e:
        logger.error(f"Неверный формат времени: {e}")
        raise ValueError("Некорректный формат времени. Пример: 09:30-18:45")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик команды /start"""
    await update.message.reply_text(
        "🕒 Введите рабочее время в формате ЧЧ:ММ-ЧЧ:ММ",
        reply_markup=get_keyboard()
    )
    return ConversationHandler.END

async def handle_work_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка введенного времени работы"""
    user_id = update.message.from_user.id
    
    try:
        hours = calculate_work_hours(update.message.text)
        user_data = data_manager.get_user_data(user_id)
        user_data['total_hours'] += hours
        user_data['last_update'] = datetime.now().date()
        
        data_manager.update_user_data(user_id, user_data)
        
        await update.message.reply_text(
            f"✅ Добавлено: {update.message.text}\n"
            f"🕒 Отработано: {hours:.2f} ч.\n"
            f"📊 Всего: {user_data['total_hours']:.2f} ч.",
            reply_markup=get_keyboard()
        )
    except ValueError as e:
        await update.message.reply_text(
            f"❌ Ошибка: {e}",
            reply_markup=get_keyboard()
        )
    
    return ConversationHandler.END

async def reset_stats_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждение сброса статистики"""
    await update.message.reply_text(
        "⚠️ Вы уверены, что хотите сбросить статистику?",
        reply_markup=ReplyKeyboardMarkup(
            [["✅ Да, сбросить", "❌ Нет, отменить"]],
            resize_keyboard=True
        )
    )
    return CONFIRM_RESET

async def execute_stats_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выполнение сброса статистики"""
    if update.message.text == "✅ Да, сбросить":
        user_id = update.message.from_user.id
        data_manager.update_user_data(user_id, data_manager._get_default_data())
        await update.message.reply_text("🔄 Статистика сброшена!", reply_markup=get_keyboard())
    else:
        await update.message.reply_text("Сброс отменён", reply_markup=get_keyboard())
    return ConversationHandler.END

async def cancel_operation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена операции"""
    await update.message.reply_text("Действие отменено", reply_markup=get_keyboard())
    return ConversationHandler.END

async def maintain_connection(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Поддержание соединения"""
    try:
        await context.bot.get_me()
        logger.debug("Соединение с Telegram активно")
    except Exception as e:
        logger.warning(f"Ошибка соединения: {e}")

def setup_application() -> Application:
    """Настройка и конфигурация приложения"""
    # Проверка обязательных переменных
    if not (token := os.getenv('TELEGRAM_TOKEN')):
        logger.error("Не задан TELEGRAM_TOKEN!")
        sys.exit(1)

    app = Application.builder().token(token).build()
    
    # Инициализация фоновых задач
    app.job_queue.run_repeating(maintain_connection, interval=PING_INTERVAL, first=10)
    
    # Настройка обработчиков
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔄 Сброс статистики$"), reset_stats_confirmation)],
        states={
            CONFIRM_RESET: [MessageHandler(filters.Regex("^(✅ Да, сбросить|❌ Нет, отменить)$"), execute_stats_reset)],
        },
        fallbacks=[CommandHandler("cancel", cancel_operation)],
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_work_time))
    
    return app

def main() -> None:
    """Точка входа в приложение"""
    try:
        # Обновление зависимостей
        subprocess.run([sys.executable, "-m", "pip", "install", "-U", "pip"], check=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], check=True)
        
        app = setup_application()
        
        logger.info("Бот запущен и готов к работе")
        app.run_polling(
            poll_interval=5.0,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}")
        time.sleep(30)
        sys.exit(1)

if __name__ == '__main__':
    main()
