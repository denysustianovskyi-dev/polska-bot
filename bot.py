"""
Telegram-бот "Флеш-картки: Польська мова"
Показує картку зі словом польською, користувач намагається згадати переклад,
потім бачить відповідь і оцінює, чи знав слово (Знав / Не знав).
Прогрес зберігається в SQLite (progress.db), щоб слова, які людина погано
знає, показувались частіше (проста spaced-repetition логіка).

Запуск:
    1) pip install -r requirements.txt
    2) створити бота через @BotFather, отримати токен
    3) встановити змінну середовища TELEGRAM_BOT_TOKEN
    4) python bot.py
"""

import json
import logging
import os
import random
import sqlite3
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

WORDS_FILE = os.path.join(os.path.dirname(__file__), "words.json")
DB_FILE = os.path.join(os.path.dirname(__file__), "progress.db")

# ---------------------------------------------------------------------------
# База даних: зберігаємо для кожного (user_id, word_id) — скільки разів
# вгадав/не вгадав, і коли слово можна показувати знову.
# ---------------------------------------------------------------------------

def db_connect():
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS progress (
            user_id INTEGER,
            word_id INTEGER,
            correct_streak INTEGER DEFAULT 0,
            next_due TEXT,
            PRIMARY KEY (user_id, word_id)
        )
        """
    )
    conn.commit()
    return conn


def load_words():
    with open(WORDS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


WORDS = load_words()  # список: {"id": int, "pl": "...", "ua": "...", "example": "..."}


def get_due_word(user_id: int):
    """Повертає слово, яке пора повторити (або нове, якщо ще не бачив)."""
    conn = db_connect()
    now = datetime.utcnow().isoformat()

    cur = conn.execute(
        "SELECT word_id FROM progress WHERE user_id=? AND next_due<=?",
        (user_id, now),
    )
    due_ids = [row[0] for row in cur.fetchall()]

    cur = conn.execute("SELECT word_id FROM progress WHERE user_id=?", (user_id,))
    seen_ids = {row[0] for row in cur.fetchall()}
    new_ids = [w["id"] for w in WORDS if w["id"] not in seen_ids]

    conn.close()

    pool = due_ids if due_ids else new_ids if new_ids else [w["id"] for w in WORDS]
    word_id = random.choice(pool)
    return next(w for w in WORDS if w["id"] == word_id)


def update_progress(user_id: int, word_id: int, knew_it: bool):
    conn = db_connect()
    cur = conn.execute(
        "SELECT correct_streak FROM progress WHERE user_id=? AND word_id=?",
        (user_id, word_id),
    )
    row = cur.fetchone()
    streak = row[0] if row else 0

    if knew_it:
        streak += 1
    else:
        streak = 0

    # інтервал росте з кожним правильним повторенням: 0хв, 10хв, 1год, 1д, 3д, 7д...
    intervals_minutes = [0, 10, 60, 60 * 24, 60 * 24 * 3, 60 * 24 * 7]
    minutes = intervals_minutes[min(streak, len(intervals_minutes) - 1)]
    next_due = (datetime.utcnow() + timedelta(minutes=minutes)).isoformat()

    conn.execute(
        """
        INSERT INTO progress (user_id, word_id, correct_streak, next_due)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, word_id)
        DO UPDATE SET correct_streak=excluded.correct_streak, next_due=excluded.next_due
        """,
        (user_id, word_id, streak, next_due),
    )
    conn.commit()
    conn.close()


def get_stats(user_id: int):
    conn = db_connect()
    cur = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN correct_streak>0 THEN 1 ELSE 0 END) "
        "FROM progress WHERE user_id=?",
        (user_id,),
    )
    seen, known = cur.fetchone()
    conn.close()
    seen = seen or 0
    known = known or 0
    return seen, known, len(WORDS)


# ---------------------------------------------------------------------------
# Хендлери команд
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привіт! 🇵🇱 Це бот для вивчення польських слів через флеш-картки.\n\n"
        "/card — отримати наступну картку\n"
        "/stats — подивитись прогрес\n\n"
        "Тисни /card, щоб почати!"
    )
    await update.message.reply_text(text)


async def send_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    word = get_due_word(user_id)
    context.user_data["current_word"] = word

    keyboard = [[InlineKeyboardButton("Показати переклад", callback_data="reveal")]]
    await update.message.reply_text(
        f"🇵🇱 *{word['pl']}*\n\nЯк це перекладається українською?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def reveal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    word = context.user_data.get("current_word")
    if not word:
        await query.edit_message_text("Спершу викликай /card")
        return

    example = f"\n\n_Приклад:_ {word['example']}" if word.get("example") else ""
    text = f"🇵🇱 *{word['pl']}*\n🇺🇦 {word['ua']}{example}\n\nТи знав це слово?"

    keyboard = [
        [
            InlineKeyboardButton("✅ Знав", callback_data="knew"),
            InlineKeyboardButton("❌ Не знав", callback_data="didnt_know"),
        ]
    ]
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    word = context.user_data.get("current_word")
    if not word:
        await query.edit_message_text("Спершу викликай /card")
        return

    knew_it = query.data == "knew"
    update_progress(update.effective_user.id, word["id"], knew_it)

    verdict = "Чудово! 🎉" if knew_it else "Нічого, повториться пізніше 💪"
    await query.edit_message_text(
        f"🇵🇱 *{word['pl']}* — {word['ua']}\n\n{verdict}\n\nТисни /card для наступної картки.",
        parse_mode="Markdown",
    )
    context.user_data.pop("current_word", None)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    seen, known, total = get_stats(update.effective_user.id)
    await update.message.reply_text(
        f"📊 Прогрес:\nПоказано слів: {seen} з {total}\n"
        f"Впевнено знаєш: {known}\n\nТисни /card, щоб продовжити!"
    )


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не знайдено TELEGRAM_BOT_TOKEN. Встанови змінну середовища перед запуском."
        )

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("card", send_card))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(reveal, pattern="^reveal$"))
    app.add_handler(CallbackQueryHandler(answer, pattern="^(knew|didnt_know)$"))

    logger.info("Бот запущено")
    app.run_polling()


if __name__ == "__main__":
    main()
