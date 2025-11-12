# bot.py
import os
import time
import logging
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# === Настройки из переменных среды или config.py ===
try:
    from config import (
        TELEGRAM_BOT_TOKEN,
        ADMIN_CHAT_ID,
        MIN_VOLUME_USD,
        CHECK_INTERVAL_SECONDS,
        WATCHLIST
    )
except ImportError:
    # Для Render.com — читаем из переменных среды
    import os
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))
    MIN_VOLUME_USD = int(os.getenv("MIN_VOLUME_USD", "1000000"))
    CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
    WATCHLIST = os.getenv("WATCHLIST", "BTCUSDT,ETHUSDT,SOLUSDT,PEPEUSDT").split(",")

# === Глобальные настройки пользователя ===
user_settings = {
    "long_percent": 3.0,
    "long_period_minutes": 3,
    "short_percent": 20.0,
    "short_period_minutes": 20,
    "dump_percent": 12.0,
    "dump_period_minutes": 4,
    "min_volume": MIN_VOLUME_USD,
    "watchlist": [s.strip() for s in WATCHLIST]
}

# === Хранение истории цен ===
price_history = {}

# === Логирование ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === Клавиатуры ===
def get_main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Биржи", callback_data="exchanges"),
         InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton("👤 Профиль", callback_data="profile"),
         InlineKeyboardButton("💳 Доступ", callback_data="access")]
    ])

def get_settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Период лонг", callback_data="set_long_period"),
         InlineKeyboardButton("➕ % лонг", callback_data="set_long_percent")],
        [InlineKeyboardButton("🔴 Период шорт", callback_data="set_short_period"),
         InlineKeyboardButton("➕ % шорт", callback_data="set_short_percent")],
        [InlineKeyboardButton("🔻 Период дамп", callback_data="set_dump_period"),
         InlineKeyboardButton("➕ % дамп", callback_data="set_dump_percent")],
        [InlineKeyboardButton("👀 Посмотреть настройки", callback_data="show_settings")],
        [InlineKeyboardButton("🔚 Назад", callback_data="back_to_menu")]
    ])

# === Binance API ===
def get_binance_ticker(symbol):
    try:
        url = f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "symbol": symbol,
                "price": float(data["lastPrice"]),
                "priceChangePercent": float(data["priceChangePercent"]),
                "volume": float(data["quoteVolume"])
            }
    except Exception as e:
        logger.error(f"Binance error for {symbol}: {e}")
    return None

# === Отправка уведомления ===
async def send_alert(context: ContextTypes.DEFAULT_TYPE, symbol: str, price: float, volume: float, signal_type: str, pct_change: float):
    emoji = {"PUMP": "🟢", "SHORT": "🟡", "DUMP": "🔴"}.get(signal_type, "🔵")
    label = {"PUMP": "Pump", "SHORT": "Short", "DUMP": "Dump"}.get(signal_type, signal_type)

    message = (
        f"{emoji} **{label}: {abs(pct_change):.2f}%** ({symbol})\n"
        f"📊 Volume: ${volume:,.0f}\n"
        f"⏱️ {datetime.utcnow().strftime('%H:%M UTC')}"
    )
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=message,
            parse_mode="Markdown"
        )
        logger.info(f"✅ Отправлен сигнал: {signal_type} {symbol} {pct_change:.2f}%")
    except Exception as e:
        logger.error(f"Ошибка отправки в Telegram: {e}")

# === Основной цикл проверки ===
async def check_signals(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    logger.info(f"🔁 Проверка {len(user_settings['watchlist'])} монет: {user_settings['watchlist']}")

    for symbol in user_settings["watchlist"]:
        # Получаем текущие данные
        ticker = get_binance_ticker(symbol)
        if not ticker:
            continue

        price = ticker["price"]
        volume = ticker["volume"]

        # Фильтр по объёму
        if volume < user_settings["min_volume"]:
            logger.debug(f"📉 {symbol} пропущен: объём {volume:,.0f} < {user_settings['min_volume']}")
            continue

        # Сохраняем цену в историю
        if symbol not in price_history:
            price_history[symbol] = []
        price_history[symbol].append({"time": now, "price": price})

        # Очищаем старые данные (>30 минут)
        cutoff = now - timedelta(minutes=30)
        price_history[symbol] = [p for p in price_history[symbol] if p["time"] > cutoff]

        # === Проверка PUMP ===
        if user_settings["long_percent"] > 0:
            past = now - timedelta(minutes=user_settings["long_period_minutes"])
            prices = [p for p in price_history[symbol] if p["time"] <= past]
            if prices:
                base_price = prices[-1]["price"]
                if price > base_price:
                    pct = (price - base_price) / base_price * 100
                    if pct >= user_settings["long_percent"]:
                        await send_alert(context, symbol, price, volume, "PUMP", pct)

        # === Проверка SHORT ===
        if user_settings["short_percent"] > 0:
            past = now - timedelta(minutes=user_settings["short_period_minutes"])
            prices = [p for p in price_history[symbol] if p["time"] <= past]
            if prices:
                base_price = prices[-1]["price"]
                if price > base_price:
                    pct = (price - base_price) / base_price * 100
                    if pct >= user_settings["short_percent"]:
                        await send_alert(context, symbol, price, volume, "SHORT", pct)

        # === Проверка DUMP ===
        if user_settings["dump_percent"] > 0:
            past = now - timedelta(minutes=user_settings["dump_period_minutes"])
            prices = [p for p in price_history[symbol] if p["time"] <= past]
            if prices:
                base_price = prices[-1]["price"]
                if price < base_price:
                    pct = (base_price - price) / base_price * 100
                    if pct >= user_settings["dump_percent"]:
                        await send_alert(context, symbol, price, volume, "DUMP", -pct)

        time.sleep(0.5)  # не перегружать API

# === Команды и кнопки ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Добро пожаловать в PUMP Screener Binance & ByBit 📈\n"
        "Пользователь Shevaserg доступ открыт до 05.12.2025 14:26",
        reply_markup=get_main_menu_keyboard()
    )

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "settings":
        msg = (
            "🤖 Я сканирую рынок на маленькие пампы (лонг 🟢), "
            "большие пампы (шорт 🔴) и резкие просадки (дамп 🔻).\n\n"
            "⚙️ Текущие настройки:\n"
            f"🟢 Лонг: {user_settings['long_percent']}% за {user_settings['long_period_minutes']} мин\n"
            f"🔴 Шорт: {user_settings['short_percent']}% за {user_settings['short_period_minutes']} мин\n"
            f"🔻 Дамп: {user_settings['dump_percent']}% за {user_settings['dump_period_minutes']} мин"
        )
        await query.edit_message_text(msg, reply_markup=get_settings_keyboard())

    elif query.data == "exchanges":
        await query.edit_message_text("📊 Биржи: Binance", reply_markup=get_main_menu_keyboard())
    elif query.data == "profile":
        await query.edit_message_text("👤 Профиль: Shevaserg", reply_markup=get_main_menu_keyboard())
    elif query.data == "access":
        await query.edit_message_text("💳 Доступ открыт до 05.12.2025 14:26", reply_markup=get_main_menu_keyboard())
    elif query.data == "back_to_menu":
        await query.edit_message_text(
            "🚀 Добро пожаловать в PUMP Screener Binance & ByBit 📈\n"
            "Пользователь Shevaserg доступ открыт до 05.12.2025 14:26",
            reply_markup=get_main_menu_keyboard()
        )
    elif query.data == "show_settings":
        msg = (
            f"🟢 Лонг: {user_settings['long_percent']}% за {user_settings['long_period_minutes']} мин\n"
            f"🔴 Шорт: {user_settings['short_percent']}% за {user_settings['short_period_minutes']} мин\n"
            f"🔻 Дамп: {user_settings['dump_percent']}% за {user_settings['dump_period_minutes']} мин"
        )
        await query.edit_message_text(msg, reply_markup=get_settings_keyboard())

    elif query.data in ["set_long_period", "set_long_percent", "set_short_period", "set_short_percent", "set_dump_period", "set_dump_percent"]:
        context.user_data["awaiting_input"] = query.data
        labels = {
            "set_long_period": "период лонга (мин)",
            "set_long_percent": "процент лонга (%)",
            "set_short_period": "период шорта (мин)",
            "set_short_percent": "процент шорта (%)",
            "set_dump_period": "период дампа (мин)",
            "set_dump_percent": "процент дампа (%)",
        }
        await query.edit_message_text(f"✏️ Введите {labels[query.data]}:")

# === Обработка ввода чисел ===
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "awaiting_input" not in context.user_data:
        return

    try:
        value = float(update.message.text)
        key = context.user_data["awaiting_input"]

        if key == "set_long_period":
            user_settings["long_period_minutes"] = max(1, int(value))
        elif key == "set_long_percent":
            user_settings["long_percent"] = value
        elif key == "set_short_period":
            user_settings["short_period_minutes"] = max(1, int(value))
        elif key == "set_short_percent":
            user_settings["short_percent"] = value
        elif key == "set_dump_period":
            user_settings["dump_period_minutes"] = max(1, int(value))
        elif key == "set_dump_percent":
            user_settings["dump_percent"] = value

        await update.message.reply_text("✅ Настройка обновлена!")
        del context.user_data["awaiting_input"]

    except ValueError:
        await update.message.reply_text("❌ Введите число (например: 3.5)")

# === Команда /add для множества монет ===
async def add_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /add BTC ETH SOL")
        return

    added = []
    for arg in context.args:
        symbol = arg.upper().replace("USDT", "") + "USDT"
        if symbol not in user_settings["watchlist"]:
            user_settings["watchlist"].append(symbol)
            added.append(symbol)

    if added:
        await update.message.reply_text(f"✅ Добавлено: {', '.join(added)}")
        logger.info(f"Добавлены монеты: {added}")
    else:
        await update.message.reply_text("⚠️ Все монеты уже в списке.")

# === Запуск ===
def main():
    if not TELEGRAM_BOT_TOKEN or not ADMIN_CHAT_ID:
        logger.error("❌ Не заданы TELEGRAM_BOT_TOKEN или ADMIN_CHAT_ID")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_coin))
    application.add_handler(CallbackQueryHandler(menu_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    job_queue = application.job_queue
    job_queue.run_repeating(check_signals, interval=CHECK_INTERVAL_SECONDS)

    logger.info("✅ Бот запущен! Напиши ему /start в Telegram.")
    application.run_polling()

if __name__ == "__main__":
    main()