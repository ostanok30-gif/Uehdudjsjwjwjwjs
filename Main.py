import asyncio
import logging
import sqlite3
import json
import os
import time
import html

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# ==============================
# CONFIG
# ==============================

CONFIG_FILE = "vocma_config.json"
DB_FILE = "vocma.sqlite3"
SUPPORT_GROUP_ID = -1004389018857
AUTO_CLOSE_SECONDS = 6 * 60 * 60
ANTI_SPAM_LIMIT = 5  # сообщений в секунду (альбомы из нескольких фото тоже проходят)


def load_dotenv(path=".env"):
    """Простой разбор .env (KEY=VALUE), без внешних библиотек."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def load_config():
    # Приоритет: .env / переменные окружения -> vocma_config.json -> ввод вручную
    load_dotenv()
    env_token = os.getenv("BOT_TOKEN")
    env_owner = os.getenv("OWNER_ID")
    if env_token and env_owner:
        return {"BOT_TOKEN": env_token, "OWNER_ID": int(env_owner)}

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    print("Введите токен Telegram-бота:")
    token = input("> ").strip()

    print("Введите ваш Telegram ID:")
    owner_id = int(input("> ").strip())

    config = {
        "BOT_TOKEN": token,
        "OWNER_ID": owner_id
    }

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f)

    return config


CONFIG = load_config()
BOT_TOKEN = CONFIG["BOT_TOKEN"]
OWNER_ID = int(CONFIG["OWNER_ID"])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# ИСПРАВЛЕНО: без parse_mode HTML-теги (<b>, <code>) отображались как текст
bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ==============================
# PREMIUM EMOJI
# ==============================
# Пак @emoji1. Формат: имя -> (custom_emoji_id, обычный эмодзи-запасной вариант).
# Если премиум-эмодзи не отобразится, пользователь увидит обычный.

PREMIUM_EMOJI = {
    "wave":     ("5472429943088490628", "👋"),
    "support":  ("5470029545996299058", "📞"),
    "ticket":   ("5472033358693280429", "💻"),
    "pencil":   ("5472410705929971383", "📖"),
    "ok":       ("5472043065319366951", "👍"),
    "heart":    ("5470092785094765546", "❤️"),
    "smile":    ("5471937988944470973", "😊"),
    "pray":     ("5472056620236154358", "🙏"),
    "user":     ("5472083261918291121", "😎"),
    "flag":     ("5471915062409045438", "🚩"),
    "closed":   ("5472201313389389629", "🥶"),
    "reopen":   ("5472075823034934400", "☕️"),
    "timer":    ("5472326412901819608", "😵"),
    "ban":      ("5472250203002120205", "😠"),
    "mute":     ("5470082021906720301", "😴"),
    "unmute":   ("5472329969134740802", "😆"),
    "stats":    ("5471961838897866467", "📰"),
    "owner":    ("5472163337288557993", "😍"),
    "error":    ("5472393762283989489", "💔"),
    "warn":     ("5472286594260018622", "👅"),
    "gift":     ("5472147707902567316", "🎁"),
    "headset":  ("5472077768655119767", "🎧"),
}


def e(name: str) -> str:
    """Возвращает HTML-тег премиум-эмодзи для вставки в текст сообщения."""
    emoji_id, fallback = PREMIUM_EMOJI[name]
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


# ==============================
# DATABASE
# ==============================

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row

db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    title TEXT,
    status TEXT DEFAULT 'open',
    created_at REAL,
    last_user_at REAL,
    closed_at REAL,
    auto_closed INTEGER DEFAULT 0,
    first_reply_at REAL
);

CREATE TABLE IF NOT EXISTS message_map (
    group_msg_id INTEGER PRIMARY KEY,
    private_msg_id INTEGER,
    ticket_id INTEGER,
    user_id INTEGER
);

CREATE TABLE IF NOT EXISTS staff (
    tg_id INTEGER PRIMARY KEY,
    role TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS restrictions (
    user_id INTEGER,
    kind TEXT,
    until_ts REAL,
    reason TEXT,
    PRIMARY KEY(user_id, kind)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT,
    user_id INTEGER,
    ticket_id INTEGER,
    created_at REAL
);
""")
db.commit()


def execute(query, params=()):
    cur = db.execute(query, params)
    db.commit()
    return cur


def one(query, params=()):
    return db.execute(query, params).fetchone()


def many(query, params=()):
    return db.execute(query, params).fetchall()


def log_event(event, user_id=None, ticket_id=None):
    execute(
        "INSERT INTO events(event,user_id,ticket_id,created_at) VALUES(?,?,?,?)",
        (event, user_id, ticket_id, time.time())
    )


def save_user(user):
    execute("""
        INSERT INTO users(tg_id,username,first_name,last_name)
        VALUES(?,?,?,?)
        ON CONFLICT(tg_id) DO UPDATE SET
        username=excluded.username,
        first_name=excluded.first_name,
        last_name=excluded.last_name
    """, (
        user.id,
        user.username,
        user.first_name,
        user.last_name
    ))


# Владелец всегда имеет высшую роль.
execute(
    "INSERT OR REPLACE INTO staff(tg_id,role) VALUES(?,?)",
    (OWNER_ID, "owner")
)


def get_role(user_id):
    if user_id == OWNER_ID:
        return "owner"

    row = one("SELECT role FROM staff WHERE tg_id=?", (user_id,))
    return row["role"] if row else None


def is_staff(user_id):
    return get_role(user_id) in ("owner", "admin", "moderator")


def can_moderate(user_id):
    return get_role(user_id) in ("owner", "admin")


def get_ticket(ticket_id):
    return one("SELECT * FROM tickets WHERE id=?", (ticket_id,))


def get_active_ticket(user_id):
    return one("""
        SELECT * FROM tickets
        WHERE user_id=? AND status='open'
        ORDER BY id DESC LIMIT 1
    """, (user_id,))


def get_last_ticket(user_id):
    return one("""
        SELECT * FROM tickets
        WHERE user_id=?
        ORDER BY id DESC LIMIT 1
    """, (user_id,))


def get_ticket_by_thread(thread_id):
    return one("""
        SELECT * FROM tickets
        WHERE thread_id=?
        ORDER BY id DESC LIMIT 1
    """, (thread_id,))


# ==============================
# FSM
# ==============================

class TicketForm(StatesGroup):
    description = State()


class StaffForm(StatesGroup):
    user_id = State()
    role = State()


# ==============================
# KEYBOARDS
# ==============================
# Цвет кнопок (style) и премиум-эмодзи на кнопках (icon_custom_emoji_id)
# появились в Bot API 9.4. Поля передаются напрямую, поэтому работает и на
# старых версиях aiogram. Если Telegram ругается на кнопки (например, у
# владельца бота нет Premium) — поставьте BUTTON_PREMIUM_ICONS = False.

BUTTON_PREMIUM_ICONS = True
BUTTON_COLORS = True


def btn(text, callback_data, style=None, icon=None):
    """style: 'primary' (синяя), 'success' (зелёная), 'danger' (красная)."""
    extra = {}
    if BUTTON_COLORS and style:
        extra["style"] = style
    if BUTTON_PREMIUM_ICONS and icon:
        extra["icon_custom_emoji_id"] = PREMIUM_EMOJI[icon][0]
    return InlineKeyboardButton(text=text, callback_data=callback_data, **extra)


def degrade_buttons():
    """Если Telegram отклонил кнопки — отключаем иконки, потом цвета."""
    global BUTTON_PREMIUM_ICONS, BUTTON_COLORS
    if BUTTON_PREMIUM_ICONS:
        BUTTON_PREMIUM_ICONS = False
        logging.warning("Иконки на кнопках отключены (Telegram их отклонил)")
        return True
    if BUTTON_COLORS:
        BUTTON_COLORS = False
        logging.warning("Цвета кнопок отключены (Telegram их отклонил)")
        return True
    return False


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Создать обращение", "new_ticket", "success", "ticket")]
    ])


def close_keyboard(ticket_id, user_id=None):
    rows = [[btn("Закрыть тикет", f"close:{ticket_id}", "danger", "closed")]]
    if user_id:
        rows.append([
            btn("Бан", f"ban:{user_id}", "danger", "ban"),
            btn("Разбан", f"unban:{user_id}", "success", "ok"),
        ])
        rows.append([
            btn("Мут 1ч", f"mute:{user_id}:60", "primary", "mute"),
            btn("Мут 24ч", f"mute:{user_id}:1440", "primary", "mute"),
            btn("Размут", f"unmute:{user_id}", "success", "unmute"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reopen_keyboard(ticket_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Открыть тикет заново", f"reopen:{ticket_id}", "primary", "reopen")]
    ])


def owner_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Добавить сотрудника", "staff_add", "success", "ok")],
        [btn("Удалить сотрудника", "staff_remove", "danger", "ban")],
        [btn("Список сотрудников", "staff_list", "primary", "user")],
        [btn("Статистика", "statistics", "primary", "stats")]
    ])


def staff_role_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Администратор", "role_admin", "primary", "owner")],
        [btn("Модератор", "role_moderator", "success", "smile")]
    ])


# ==============================
# RATE LIMIT
# ==============================

user_activity = {}


def rate_limited(user_id):
    now = time.monotonic()
    timestamps = [t for t in user_activity.get(user_id, []) if now - t < 1]

    if len(timestamps) >= ANTI_SPAM_LIMIT:
        user_activity[user_id] = timestamps
        return True

    timestamps.append(now)
    user_activity[user_id] = timestamps
    return False


# ==============================
# RESTRICTIONS
# ==============================

def get_restriction(user_id, kind):
    row = one(
        "SELECT * FROM restrictions WHERE user_id=? AND kind=?",
        (user_id, kind)
    )

    if not row:
        return None

    if row["until_ts"] and row["until_ts"] <= time.time():
        execute(
            "DELETE FROM restrictions WHERE user_id=? AND kind=?",
            (user_id, kind)
        )
        return None

    return row


def is_banned(user_id):
    return get_restriction(user_id, "ban") is not None


def is_muted(user_id):
    return get_restriction(user_id, "mute") is not None


# ==============================
# START / CREATE TICKET
# ==============================

@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start(message: Message, state: FSMContext):
    await state.clear()  # сбрасываем незавершённую форму
    save_user(message.from_user)

    ticket = get_active_ticket(message.from_user.id)

    if ticket:
        await message.answer(
            f"{e('ok')} У вас уже есть открытое обращение.\n"
            "Напишите сообщение, и оно попадёт в вашу тему поддержки."
        )
        return

    last = get_last_ticket(message.from_user.id)

    if last:
        await message.answer(
            f"{e('closed')} Ваше обращение закрыто.\n"
            "Вы можете открыть его заново:",
            reply_markup=reopen_keyboard(last["id"])
        )
        return

    await message.answer(
        f"{e('wave')} <b>Добро пожаловать в поддержку Vocma!</b>\n\n"
        "Нажмите кнопку ниже, чтобы создать обращение.",
        reply_markup=main_keyboard()
    )


@router.callback_query(F.data == "new_ticket")
async def new_ticket(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id

    if is_banned(user_id):
        await callback.answer("Доступ к поддержке ограничен.", show_alert=True)
        return

    if get_active_ticket(user_id):
        await callback.message.answer(
            f"{e('ok')} У вас уже есть открытый тикет. Напишите сообщение сюда."
        )
        await callback.answer()
        return

    last = get_last_ticket(user_id)

    if last:
        await callback.message.answer(
            f"{e('closed')} У вас уже есть обращение. "
            "Новое создать нельзя, но можно открыть его заново:",
            reply_markup=reopen_keyboard(last["id"])
        )
        await callback.answer()
        return

    await state.set_state(TicketForm.description)
    await callback.message.answer(
        f"{e('headset')} Подробно опишите вашу проблему."
    )
    await callback.answer()


@router.message(TicketForm.description)
async def ticket_description(message: Message, state: FSMContext):
    user_id = message.from_user.id

    if is_banned(user_id):
        await state.clear()
        await message.answer(f"{e('ban')} Доступ к поддержке ограничен.")
        return

    if get_active_ticket(user_id):
        await state.clear()
        await message.answer("У вас уже есть открытый тикет.")
        return

    last = get_last_ticket(user_id)
    if last:
        await state.clear()
        await message.answer(
            f"{e('closed')} У вас уже есть обращение. "
            "Откройте его заново:",
            reply_markup=reopen_keyboard(last["id"])
        )
        return

    now = time.time()

    # Сначала создаём тикет (thread_id пока 0), чтобы узнать его номер
    cursor = execute("""
        INSERT INTO tickets(
            user_id, thread_id, title, status,
            created_at, last_user_at
        )
        VALUES(?,?,?,?,?,?)
    """, (user_id, 0, None, "open", now, now))

    ticket_id = cursor.lastrowid

    username = (
        f"@{message.from_user.username}"
        if message.from_user.username else "нет"
    )

    # Название темы: "#1 | @username" (или имя, если username нет)
    topic_user = (
        f"@{message.from_user.username}"
        if message.from_user.username else message.from_user.full_name
    )
    topic_name = f"#{ticket_id} | {topic_user}"[:128]

    try:
        topic = await bot.create_forum_topic(
            chat_id=SUPPORT_GROUP_ID,
            name=topic_name
        )
    except Exception:
        logging.exception("Не удалось создать тему")
        execute("DELETE FROM tickets WHERE id=?", (ticket_id,))
        await message.answer(
            f"{e('error')} Не удалось создать тикет. "
            "Попробуйте позже."
        )
        await state.clear()
        return

    execute(
        "UPDATE tickets SET thread_id=? WHERE id=?",
        (topic.message_thread_id, ticket_id)
    )

    card_text = (
        f"{e('ticket')} <b>Новое обращение #{ticket_id}</b>\n\n"
        f"{e('user')} <b>Пользователь:</b> "
        f"{html.escape(message.from_user.full_name)}\n"
        f"{e('smile')} <b>Username:</b> {html.escape(username)}\n"
        f"{e('flag')} <b>ID:</b> <code>{user_id}</code>\n"
        f"{e('ok')} <b>Статус:</b> Открыт"
    )

    card = None
    for _ in range(3):
        try:
            card = await bot.send_message(
                SUPPORT_GROUP_ID,
                card_text,
                message_thread_id=topic.message_thread_id,
                reply_markup=close_keyboard(ticket_id, user_id)
            )
            break
        except TelegramBadRequest as exc:
            logging.warning("Карточка не отправилась: %s", exc)
            if not degrade_buttons():
                break

    if card is None:
        # Последняя попытка: без кнопок, чтобы карточка и закрепление были
        card = await bot.send_message(
            SUPPORT_GROUP_ID,
            card_text,
            message_thread_id=topic.message_thread_id
        )

    try:
        pinned = await bot.pin_chat_message(
            SUPPORT_GROUP_ID,
            card.message_id,
            disable_notification=True
        )
        logging.info(
            "pinChatMessage: result=%s chat=%s message_id=%s thread=%s",
            pinned, SUPPORT_GROUP_ID, card.message_id,
            topic.message_thread_id
        )
    except Exception as exc:
        # Раньше ошибка глушилась молча. Чаще всего у бота нет права
        # «Закрепление сообщений» в группе.
        logging.warning("Не удалось закрепить карточку тикета: %s", exc)
        try:
            await bot.send_message(
                SUPPORT_GROUP_ID,
                f"{e('warn')} Не удалось закрепить карточку: {html.escape(str(exc))}\n"
                "Выдайте боту право «Закрепление сообщений» в группе.",
                message_thread_id=topic.message_thread_id
            )
        except Exception:
            pass

    copied = await bot.copy_message(
        SUPPORT_GROUP_ID,
        message.chat.id,
        message.message_id,
        message_thread_id=topic.message_thread_id
    )

    execute("""
        INSERT OR REPLACE INTO message_map(
            group_msg_id, private_msg_id, ticket_id, user_id
        ) VALUES(?,?,?,?)
    """, (copied.message_id, message.message_id, ticket_id, user_id))

    log_event("new_ticket", user_id, ticket_id)

    await message.answer(
        f"{e('heart')} <b>Обращение принято!</b>\n\n"
        "Мы постараемся ответить в течение 24 часов.\n"
        "Вы можете отправлять дополнительные сообщения сюда."
    )

    await state.clear()


# ==============================
# CLOSE TICKET
# ==============================

@router.callback_query(F.data.startswith("close:"))
async def close_ticket(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)

    if not ticket or ticket["status"] != "open":
        await callback.answer("Тикет уже закрыт")
        return

    execute(
        "UPDATE tickets SET status='closed', closed_at=? WHERE id=?",
        (time.time(), ticket_id)
    )

    log_event("manual_close", ticket["user_id"], ticket_id)

    try:
        await bot.send_message(
            ticket["user_id"],
            f"{e('closed')} <b>Ваше обращение закрыто.</b>\n"
            "Если вам снова нужна помощь, нажмите кнопку ниже.",
            reply_markup=reopen_keyboard(ticket_id)
        )
    except Exception:
        logging.exception("Не удалось уведомить пользователя")

    await callback.message.answer(f"{e('closed')} Тикет закрыт.")
    await callback.answer("Закрыто")


# ==============================
# REOPEN TICKET
# ==============================

@router.callback_query(F.data.startswith("reopen:"))
async def reopen_ticket(callback: CallbackQuery):
    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)

    if not ticket or ticket["user_id"] != callback.from_user.id:
        await callback.answer("Тикет не найден", show_alert=True)
        return

    if ticket["status"] == "open":
        await callback.answer("Тикет уже открыт")
        return

    if is_banned(callback.from_user.id):
        await callback.answer("Доступ ограничен", show_alert=True)
        return

    if get_active_ticket(callback.from_user.id):
        await callback.answer(
            "У вас уже есть другое открытое обращение",
            show_alert=True
        )
        return

    execute("""
        UPDATE tickets
        SET status='open',
            closed_at=NULL,
            last_user_at=?,
            auto_closed=0
        WHERE id=?
    """, (time.time(), ticket_id))

    log_event("reopen", callback.from_user.id, ticket_id)

    try:
        await bot.send_message(
            SUPPORT_GROUP_ID,
            f"{e('reopen')} Пользователь повторно открыл обращение.",
            message_thread_id=ticket["thread_id"]
        )
    except Exception:
        logging.exception("Не удалось уведомить группу о переоткрытии")

    await callback.message.answer(
        f"{e('ok')} Обращение снова открыто. Напишите сообщение сюда."
    )
    await callback.answer("Тикет открыт")


# ==============================
# MODERATION BUTTONS (в карточке тикета)
# ==============================

async def _mod_check(callback: CallbackQuery, user_id: int) -> bool:
    if not can_moderate(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return False
    if user_id == OWNER_ID or is_staff(user_id):
        await callback.answer("Нельзя применить к сотруднику", show_alert=True)
        return False
    return True


async def _mod_report(callback: CallbackQuery, text: str):
    who = html.escape(callback.from_user.full_name)
    await callback.message.answer(f"{text}\n<i>Сотрудник: {who}</i>")


@router.callback_query(F.data.startswith("ban:"))
async def cb_ban(callback: CallbackQuery):
    user_id = int(callback.data.split(":")[1])
    if not await _mod_check(callback, user_id):
        return

    execute("""
        INSERT OR REPLACE INTO restrictions
        (user_id,kind,until_ts,reason)
        VALUES(?,?,?,?)
    """, (user_id, "ban", None, "Административная блокировка"))
    log_event("ban", user_id)

    await callback.answer("Заблокирован")
    await _mod_report(callback, f"{e('ban')} Пользователь {user_id} заблокирован.")
    try:
        await bot.send_message(user_id, f"{e('ban')} Ваш доступ к поддержке ограничен.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("unban:"))
async def cb_unban(callback: CallbackQuery):
    user_id = int(callback.data.split(":")[1])
    if not can_moderate(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    execute("DELETE FROM restrictions WHERE user_id=? AND kind='ban'", (user_id,))
    log_event("unban", user_id)

    await callback.answer("Разбанен")
    await _mod_report(callback, f"{e('ok')} Блокировка пользователя {user_id} снята.")
    try:
        await bot.send_message(user_id, f"{e('ok')} Доступ к поддержке восстановлен.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("mute:"))
async def cb_mute(callback: CallbackQuery):
    _, uid, minutes = callback.data.split(":")
    user_id, minutes = int(uid), int(minutes)
    if not await _mod_check(callback, user_id):
        return

    execute("""
        INSERT OR REPLACE INTO restrictions
        (user_id,kind,until_ts,reason)
        VALUES(?,?,?,?)
    """, (user_id, "mute", time.time() + minutes * 60, "Административный мут"))
    log_event("mute", user_id)

    await callback.answer("Замучен")
    await _mod_report(
        callback,
        f"{e('mute')} Пользователь {user_id} замучен на {minutes} мин."
    )
    try:
        await bot.send_message(
            user_id,
            f"{e('mute')} Отправка сообщений временно ограничена на {minutes} минут."
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("unmute:"))
async def cb_unmute(callback: CallbackQuery):
    user_id = int(callback.data.split(":")[1])
    if not can_moderate(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    execute("DELETE FROM restrictions WHERE user_id=? AND kind='mute'", (user_id,))
    log_event("unmute", user_id)

    await callback.answer("Мут снят")
    await _mod_report(callback, f"{e('unmute')} Мут пользователя {user_id} снят.")


# ==============================
# AUTO CLOSE
# ==============================

async def auto_close_worker():
    while True:
        await asyncio.sleep(30)

        try:
            limit = time.time() - AUTO_CLOSE_SECONDS
            tickets = many("""
                SELECT * FROM tickets
                WHERE status='open' AND last_user_at<=?
            """, (limit,))
        except Exception:
            logging.exception("Ошибка выборки тикетов для автозакрытия")
            continue

        for ticket in tickets:
            try:
                execute("""
                    UPDATE tickets
                    SET status='closed',
                        closed_at=?,
                        auto_closed=1
                    WHERE id=? AND status='open'
                """, (time.time(), ticket["id"]))

                log_event("auto_close", ticket["user_id"], ticket["id"])
            except Exception:
                logging.exception("Ошибка автоматического закрытия")
                continue

            # Уведомления в отдельных try: сбой одного не отменяет другое
            try:
                await bot.send_message(
                    ticket["user_id"],
                    f"{e('timer')} Обращение автоматически закрыто "
                    "из-за отсутствия новых сообщений.\n\n"
                    "Вы можете открыть его снова:",
                    reply_markup=reopen_keyboard(ticket["id"])
                )
            except Exception:
                logging.warning("Не удалось уведомить пользователя об автозакрытии")

            try:
                await bot.send_message(
                    SUPPORT_GROUP_ID,
                    f"{e('timer')} Тикет автоматически закрыт "
                    "после 6 часов без сообщений пользователя.",
                    message_thread_id=ticket["thread_id"]
                )
            except Exception:
                logging.warning("Не удалось уведомить группу об автозакрытии")


# ==============================
# BAN / UNBAN
# ==============================

def target_from(message, args):
    """ID из аргумента или, если команда в теме тикета, ID автора тикета."""
    if len(args) >= 2 and args[1].strip().isdigit():
        return int(args[1].strip())
    if message.chat.id == SUPPORT_GROUP_ID and message.message_thread_id:
        t = get_ticket_by_thread(message.message_thread_id)
        if t:
            return t["user_id"]
    return None


@router.message(Command("ban"))
async def ban_user(message: Message):
    if not can_moderate(message.from_user.id):
        return

    args = (message.text or "").split(maxsplit=1)

    user_id = target_from(message, args)
    if user_id is None:
        await message.reply("Использование: /ban USER_ID (или просто /ban в теме тикета)")
        return

    if user_id == OWNER_ID or is_staff(user_id):
        await message.reply("Нельзя заблокировать сотрудника.")
        return

    execute("""
        INSERT OR REPLACE INTO restrictions
        (user_id,kind,until_ts,reason)
        VALUES(?,?,?,?)
    """, (user_id, "ban", None, "Административная блокировка"))

    log_event("ban", user_id)

    await message.reply(f"{e('ban')} Пользователь {user_id} заблокирован.")

    try:
        await bot.send_message(
            user_id,
            f"{e('ban')} Ваш доступ к поддержке ограничен."
        )
    except Exception:
        pass


@router.message(Command("unban"))
async def unban_user(message: Message):
    if not can_moderate(message.from_user.id):
        return

    args = (message.text or "").split(maxsplit=1)

    user_id = target_from(message, args)
    if user_id is None:
        await message.reply("Использование: /unban USER_ID (или просто /unban в теме тикета)")
        return

    execute(
        "DELETE FROM restrictions WHERE user_id=? AND kind='ban'",
        (user_id,)
    )

    log_event("unban", user_id)

    await message.reply(f"{e('ok')} Блокировка пользователя {user_id} снята.")


# ==============================
# MUTE / UNMUTE
# ==============================

@router.message(Command("mute"))
async def mute_user(message: Message):
    if not can_moderate(message.from_user.id):
        return

    args = (message.text or "").split()

    if len(args) != 3 or not args[1].isdigit() or not args[2].isdigit():
        await message.reply("Использование: /mute USER_ID MINUTES")
        return

    user_id = int(args[1])
    minutes = int(args[2])

    if minutes <= 0:
        await message.reply("Укажите положительное число минут.")
        return

    if user_id == OWNER_ID or is_staff(user_id):
        await message.reply("Нельзя замутить сотрудника.")
        return

    until_ts = time.time() + minutes * 60

    execute("""
        INSERT OR REPLACE INTO restrictions
        (user_id,kind,until_ts,reason)
        VALUES(?,?,?,?)
    """, (user_id, "mute", until_ts, "Административный мут"))

    log_event("mute", user_id)

    await message.reply(
        f"{e('mute')} Пользователь {user_id} замучен на {minutes} минут."
    )

    try:
        await bot.send_message(
            user_id,
            f"{e('mute')} Отправка сообщений временно ограничена на {minutes} минут."
        )
    except Exception:
        pass


@router.message(Command("unmute"))
async def unmute_user(message: Message):
    if not can_moderate(message.from_user.id):
        return

    args = (message.text or "").split(maxsplit=1)

    if len(args) < 2 or not args[1].strip().isdigit():
        await message.reply("Использование: /unmute USER_ID")
        return

    user_id = int(args[1].strip())

    execute(
        "DELETE FROM restrictions WHERE user_id=? AND kind='mute'",
        (user_id,)
    )

    log_event("unmute", user_id)

    await message.reply(f"{e('unmute')} Мут пользователя {user_id} снят.")


# ==============================
# OWNER MENU
# ==============================

@router.message(Command("owner"))
async def owner_menu(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    await state.clear()
    await message.answer(
        f"{e('owner')} <b>Панель владельца Vocma</b>",
        reply_markup=owner_keyboard()
    )


@router.callback_query(F.data == "staff_add")
async def staff_add(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Только владелец", show_alert=True)
        return

    await state.set_state(StaffForm.user_id)
    await callback.message.answer("Введите Telegram ID сотрудника:")
    await callback.answer()


@router.message(StaffForm.user_id)
async def staff_user_id(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        await state.clear()
        return

    if not message.text or not message.text.strip().isdigit():
        await message.answer("Введите числовой Telegram ID.")
        return

    user_id = int(message.text.strip())

    if user_id == OWNER_ID:
        await message.answer("Владелец не может быть изменён.")
        await state.clear()
        return

    await state.update_data(user_id=user_id)
    await state.set_state(StaffForm.role)

    await message.answer(
        "Выберите роль сотрудника:",
        reply_markup=staff_role_keyboard()
    )


@router.callback_query(F.data.in_(["role_admin", "role_moderator"]))
async def staff_set_role(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Только владелец", show_alert=True)
        return

    data = await state.get_data()
    user_id = data.get("user_id")

    if not user_id:
        await callback.answer("Сначала укажите ID")
        return

    role = "admin" if callback.data == "role_admin" else "moderator"

    execute(
        "INSERT OR REPLACE INTO staff(tg_id,role) VALUES(?,?)",
        (user_id, role)
    )

    await state.clear()
    await callback.message.answer(
        f"{e('ok')} Сотрудник {user_id} добавлен. Роль: {role}."
    )
    await callback.answer()


@router.callback_query(F.data == "staff_remove")
async def staff_remove(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Только владелец", show_alert=True)
        return

    await callback.message.answer(
        "Для удаления сотрудника отправьте команду:\n"
        "/remove_staff TELEGRAM_ID"
    )
    await callback.answer()


@router.message(Command("remove_staff"))
async def remove_staff(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    args = (message.text or "").split(maxsplit=1)

    if len(args) != 2 or not args[1].strip().isdigit():
        await message.answer("Использование: /remove_staff TELEGRAM_ID")
        return

    user_id = int(args[1].strip())

    if user_id == OWNER_ID:
        await message.answer("Нельзя удалить владельца.")
        return

    execute("DELETE FROM staff WHERE tg_id=?", (user_id,))
    await message.answer(f"{e('ok')} Сотрудник {user_id} удалён.")


@router.callback_query(F.data == "staff_list")
async def staff_list(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Только владелец", show_alert=True)
        return

    rows = many("SELECT tg_id,role FROM staff ORDER BY role,tg_id")

    role_names = {
        "owner": "Владелец",
        "admin": "Администратор",
        "moderator": "Модератор"
    }

    text = f"{e('user')} <b>Сотрудники Vocma</b>\n\n"

    for row in rows:
        text += (
            f"{e('flag')} <code>{row['tg_id']}</code> — "
            f"{role_names.get(row['role'], row['role'])}\n"
        )

    await callback.message.answer(text)
    await callback.answer()


# ==============================
# STATISTICS
# ==============================

@router.message(Command("stats"))
async def stats_command(message: Message):
    if not can_moderate(message.from_user.id):
        return

    await send_stats(message)


@router.callback_query(F.data == "statistics")
async def stats_callback(callback: CallbackQuery):
    if not can_moderate(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await send_stats(callback.message)
    await callback.answer()


async def send_stats(message):
    total = one("SELECT COUNT(*) AS n FROM tickets")["n"]
    opened = one("SELECT COUNT(*) AS n FROM tickets WHERE status='open'")["n"]
    closed = one("SELECT COUNT(*) AS n FROM tickets WHERE status='closed'")["n"]
    auto_closed = one("SELECT COUNT(*) AS n FROM tickets WHERE auto_closed=1")["n"]

    now = time.time()

    today = one(
        "SELECT COUNT(*) AS n FROM tickets WHERE created_at>=?",
        (now - 86400,)
    )["n"]
    week = one(
        "SELECT COUNT(*) AS n FROM tickets WHERE created_at>=?",
        (now - 7 * 86400,)
    )["n"]
    month = one(
        "SELECT COUNT(*) AS n FROM tickets WHERE created_at>=?",
        (now - 30 * 86400,)
    )["n"]

    response = one("""
        SELECT AVG(first_reply_at-created_at) AS avg_time
        FROM tickets WHERE first_reply_at IS NOT NULL
    """)["avg_time"]

    # Считаем только активные ограничения (истёкшие муты не учитываем)
    bans = one(
        "SELECT COUNT(*) AS n FROM restrictions "
        "WHERE kind='ban' AND (until_ts IS NULL OR until_ts>?)",
        (now,)
    )["n"]
    mutes = one(
        "SELECT COUNT(*) AS n FROM restrictions "
        "WHERE kind='mute' AND (until_ts IS NULL OR until_ts>?)",
        (now,)
    )["n"]

    avg_text = (
        f"{int(response // 3600)} ч. "
        f"{int((response % 3600) // 60)} мин."
        if response else "Нет данных"
    )

    await message.answer(
        f"{e('stats')} <b>Статистика Vocma Support</b>\n\n"
        f"{e('ticket')} Всего тикетов: {total}\n"
        f"{e('ok')} Открытых: {opened}\n"
        f"{e('closed')} Закрытых: {closed}\n"
        f"{e('timer')} Автозакрытых: {auto_closed}\n\n"
        f"{e('flag')} Сегодня: {today}\n"
        f"{e('flag')} За неделю: {week}\n"
        f"{e('flag')} За месяц: {month}\n\n"
        f"{e('reopen')} Среднее время первого ответа: {avg_text}\n\n"
        f"{e('ban')} Бан: {bans}\n"
        f"{e('mute')} Мут: {mutes}"
    )


# ==============================
# USER MESSAGES (в личке)
# ==============================
# ВАЖНО: этот хендлер ловит ВСЕ личные сообщения, поэтому он должен
# стоять ПОСЛЕ всех команд и FSM-хендлеров. Раньше он стоял выше и
# "съедал" /owner, /stats и ввод ID сотрудника.

@router.message(
    F.chat.type == ChatType.PRIVATE,
    StateFilter(None),
    ~F.text.startswith("/")
)
async def private_message(message: Message):
    user_id = message.from_user.id

    save_user(message.from_user)

    if is_banned(user_id):
        await message.answer(f"{e('ban')} Доступ к поддержке ограничен.")
        return

    if is_muted(user_id):
        await message.answer(f"{e('mute')} Вы временно не можете отправлять сообщения.")
        return

    if rate_limited(user_id):
        await message.answer(f"{e('warn')} Не отправляйте сообщения так часто.")
        return

    ticket = get_active_ticket(user_id)

    if not ticket:
        last = get_last_ticket(user_id)
        if last:
            await message.answer(
                f"{e('closed')} Ваше обращение закрыто.",
                reply_markup=reopen_keyboard(last["id"])
            )
        else:
            await message.answer(
                "У вас нет открытого обращения.",
                reply_markup=main_keyboard()
            )
        return

    try:
        copied = await bot.copy_message(
            SUPPORT_GROUP_ID,
            message.chat.id,
            message.message_id,
            message_thread_id=ticket["thread_id"]
        )

        execute("""
            INSERT OR REPLACE INTO message_map(
                group_msg_id, private_msg_id, ticket_id, user_id
            ) VALUES(?,?,?,?)
        """, (copied.message_id, message.message_id, ticket["id"], user_id))

        execute(
            "UPDATE tickets SET last_user_at=? WHERE id=?",
            (time.time(), ticket["id"])
        )

        log_event("user_message", user_id, ticket["id"])

    except Exception:
        logging.exception("Ошибка пересылки сообщения")
        await message.answer(
            f"{e('error')} Не удалось доставить сообщение. Попробуйте позже."
        )


# ==============================
# STAFF REPLIES (в группе поддержки)
# ==============================
# Тоже стоит после команд: раньше он перехватывал /ban, /mute и т.д. в группе.

@router.message(F.chat.id == SUPPORT_GROUP_ID, ~F.text.startswith("/"))
async def staff_message(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return

    if not get_role(message.from_user.id):
        return

    if not message.message_thread_id:
        return

    ticket = get_ticket_by_thread(message.message_thread_id)

    if not ticket or ticket["status"] != "open":
        return

    user_id = ticket["user_id"]
    reply_to = None

    # Если ответ даётся Reply'ем на сообщение из тикета — отвечаем на него
    if message.reply_to_message:
        mapping = one(
            "SELECT private_msg_id, user_id FROM message_map "
            "WHERE group_msg_id=?",
            (message.reply_to_message.message_id,)
        )
        if mapping:
            user_id = mapping["user_id"]
            reply_to = mapping["private_msg_id"]

    try:
        kwargs = {}
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to
            kwargs["allow_sending_without_reply"] = True

        copied = await bot.copy_message(
            user_id,
            SUPPORT_GROUP_ID,
            message.message_id,
            **kwargs
        )

        execute("""
            INSERT OR REPLACE INTO message_map(
                group_msg_id, private_msg_id, ticket_id, user_id
            ) VALUES(?,?,?,?)
        """, (message.message_id, copied.message_id, ticket["id"], user_id))

        if not ticket["first_reply_at"]:
            execute(
                "UPDATE tickets SET first_reply_at=? WHERE id=?",
                (time.time(), ticket["id"])
            )

    except TelegramForbiddenError:
        await message.reply("Пользователь заблокировал бота.")
    except Exception:
        logging.exception("Ошибка ответа пользователю")
        await message.reply("Не удалось отправить ответ.")


# ==============================
# ERROR HANDLING
# ==============================

@router.errors()
async def errors_handler(event):
    logging.exception("Ошибка обработки Telegram update: %s", event.exception)

    # Если сообщение не ушло из-за кнопок — отключаем иконки/цвета
    exc = event.exception
    text = str(exc).lower()
    if isinstance(exc, TelegramBadRequest) and any(
        w in text for w in ("button", "icon", "style", "custom emoji")
    ):
        degrade_buttons()

    return True


# ==============================
# START BOT
# ==============================

async def main():
    me = await bot.get_me()

    print("=" * 40)
    print("VOCMA SUPPORT BOT")
    print(f"Бот: @{me.username}")
    print(f"Группа: {SUPPORT_GROUP_ID}")
    print("База данных: SQLite")
    print("=" * 40)

    asyncio.create_task(auto_close_worker())

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")