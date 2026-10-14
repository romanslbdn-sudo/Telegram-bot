"""
Телеграм-бот для запису клієнтів на прийом.

Потік:
1. Клієнт: ім'я -> телефон -> послуга -> бажаний день -> бажаний час -> коментар.
2. Бот зберігає запит і каже: «Ми повідомимо про точний час».
3. Адміністратору приходить запит з кнопками:
   ✅ Підтвердити як є | 🕒 Запропонувати інший час | ❌ Відхилити
4. Якщо запропоновано інший час, клієнт отримує кнопки «Підходить / Не підходить».
"""
import asyncio
import html
import logging
import os
import sqlite3
import threading
from datetime import date, timedelta
from flask import Flask

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------------------------------------------------------------- налаштування
BOT_TOKEN = os.environ["BOT_TOKEN"]
# ID адміністраторів через кому, напр. ADMIN_IDS=123456789,987654321
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.getenv("DB_PATH", "bookings.db")

# Відредагуйте під себе
SERVICES = ["Послуга 1", "Послуга 2", "Послуга 3", "Консультація"]
TIME_SLOTS = ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00"]
DAYS_AHEAD = 7
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]

BTN_BOOK = "📝 Записатися"

router = Router()


# ------------------------------------------------------------------------ БД
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT, phone TEXT, service TEXT,
                wanted TEXT, comment TEXT,
                status TEXT DEFAULT 'new',
                final_time TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )


def get_booking(booking_id: int):
    with db() as c:
        return c.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()


def set_status(booking_id: int, status: str, final_time: str | None = None) -> None:
    with db() as c:
        c.execute(
            "UPDATE bookings SET status = ?, final_time = COALESCE(?, final_time) WHERE id = ?",
            (status, final_time, booking_id),
        )


# --------------------------------------------------------------------- стани
class Booking(StatesGroup):
    name = State()
    phone = State()
    service = State()
    day = State()
    time = State()
    comment = State()


class Propose(StatesGroup):
    waiting_time = State()


# ------------------------------------------------------------------ допоміжне
def e(text) -> str:
    return html.escape(str(text or "—"))


def booking_text(r: sqlite3.Row) -> str:
    return (
        f"<b>Запит #{r['id']}</b>\n"
        f"👤 {e(r['name'])} (<a href=\"tg://user?id={r['user_id']}\">профіль</a>)\n"
        f"📞 {e(r['phone'])}\n"
        f"🛠 {e(r['service'])}\n"
        f"🗓 Бажаний час: <b>{e(r['wanted'])}</b>\n"
        f"💬 {e(r['comment'])}"
    )


def admin_kb(booking_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Підтвердити як є", callback_data=f"adm:ok:{booking_id}")
    kb.button(text="🕒 Інший час", callback_data=f"adm:alt:{booking_id}")
    kb.button(text="❌ Відхилити", callback_data=f"adm:no:{booking_id}")
    kb.adjust(1)
    return kb.as_markup()


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_BOOK)]], resize_keyboard=True)


def day_label(d: date) -> str:
    prefix = {0: "Сьогодні", 1: "Завтра"}.get(( d - date.today()).days)
    base = f"{d.strftime('%d.%m')} ({WEEKDAYS[d.weekday()]})"
    return f"{prefix}, {base}" if prefix else base


async def notify_admins(bot: Bot, text: str, reply_markup=None) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as exc:  # адмін міг не запускати бота
            logging.warning("Не вдалося надіслати адміну %s: %s", admin_id, exc)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# -------------------------------------------------------------- клієнт: запис
@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "Вітаємо! 👋 Я допоможу залишити запит на запис.\nНатисніть кнопку нижче.",
        reply_markup=main_kb(),
    )


@router.message(Command("book"))
@router.message(F.text == BTN_BOOK)
async def begin(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Booking.name)
    await m.answer("Як до вас звертатися?", reply_markup=ReplyKeyboardRemove())


@router.message(Booking.name, F.text)
async def got_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()[:60])
    await state.set_state(Booking.phone)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Надіслати мій номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await m.answer("Залиште номер телефону: натисніть кнопку або напишіть його.", reply_markup=kb)


async def ask_service(m: Message, state: FSMContext, phone: str):
    await state.update_data(phone=phone)
    await state.set_state(Booking.service)
    kb = InlineKeyboardBuilder()
    for i, s in enumerate(SERVICES):
        kb.button(text=s, callback_data=f"svc:{i}")
    kb.adjust(1)
    await m.answer("Оберіть послугу:", reply_markup=ReplyKeyboardRemove())
    await m.answer("👇", reply_markup=kb.as_markup())


@router.message(Booking.phone, F.contact)
async def got_contact(m: Message, state: FSMContext):
    await ask_service(m, state, m.contact.phone_number)


@router.message(Booking.phone, F.text)
async def got_phone_text(m: Message, state: FSMContext):
    phone = m.text.strip()
    if sum(ch.isdigit() for ch in phone) < 9:
        await m.answer("Схоже, номер некоректний. Спробуйте ще раз, напр. +380501234567")
        return
    await ask_service(m, state, phone)


@router.callback_query(Booking.service, F.data.startswith("svc:"))
async def got_service(cb: CallbackQuery, state: FSMContext):
    service = SERVICES[int(cb.data.split(":")[1])]
    await state.update_data(service=service)
    await state.set_state(Booking.day)
    kb = InlineKeyboardBuilder()
    today = date.today()
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        kb.button(text=day_label(d), callback_data=f"day:{d.isoformat()}")
    kb.adjust(2)
    await cb.message.edit_text(f"Послуга: <b>{e(service)}</b>\n\nОберіть бажаний день:", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(Booking.day, F.data.startswith("day:"))
async def got_day(cb: CallbackQuery, state: FSMContext):
    d = date.fromisoformat(cb.data.split(":", 1)[1])
    await state.update_data(day=day_label(d))
    await state.set_state(Booking.time)
    kb = InlineKeyboardBuilder()
    for t in TIME_SLOTS:
        kb.button(text=t, callback_data=f"time:{t}")
    kb.button(text="Будь-який час", callback_data="time:будь-який час")
    kb.adjust(3)
    await cb.message.edit_text(
        f"День: <b>{day_label(d)}</b>\n\nОберіть бажаний час (точний час ми підтвердимо окремо):",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@router.callback_query(Booking.time, F.data.startswith("time:"))
async def got_time(cb: CallbackQuery, state: FSMContext):
    await state.update_data(time=cb.data.split(":", 1)[1])
    await state.set_state(Booking.comment)
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустити", callback_data="skip_comment")
    await cb.message.edit_text(
        "Додайте коментар (марка авто, побажання тощо) або натисніть «Пропустити».",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


async def finalize(bot: Bot, chat_id: int, user_id: int, state: FSMContext, comment: str | None):
    data = await state.get_data()
    wanted = f"{data['day']}, {data['time']}"
    with db() as c:
        cur = c.execute(
            "INSERT INTO bookings (user_id, name, phone, service, wanted, comment) VALUES (?,?,?,?,?,?)",
            (user_id, data["name"], data["phone"], data["service"], wanted, comment),
        )
        booking_id = cur.lastrowid
    await state.clear()

    await bot.send_message(
        chat_id,
        f"✅ Дякуємо, {e(data['name'])}! Ваш запит прийнято.\n\n"
        f"🛠 {e(data['service'])}\n🗓 Бажаний час: <b>{e(wanted)}</b>\n\n"
        "Ми повідомимо вас про точний час прийому, який ми зможемо запропонувати.",
        reply_markup=main_kb(),
    )
    await notify_admins(bot, booking_text(get_booking(booking_id)), admin_kb(booking_id))


@router.message(Booking.comment, F.text)
async def got_comment(m: Message, state: FSMContext, bot: Bot):
    await finalize(bot, m.chat.id, m.from_user.id, state, m.text.strip()[:500])


@router.callback_query(Booking.comment, F.data == "skip_comment")
async def skip_comment(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.message.edit_reply_markup(reply_markup=None)
    await finalize(bot, cb.message.chat.id, cb.from_user.id, state, None)
    await cb.answer()


# ------------------------------------------------------------ адмін: рішення
@router.callback_query(F.data.startswith("adm:"))
async def admin_action(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(cb.from_user.id):
        await cb.answer("Недостатньо прав", show_alert=True)
        return
    _, action, raw_id = cb.data.split(":")
    booking_id = int(raw_id)
    r = get_booking(booking_id)
    if not r:
        await cb.answer("Запис не знайдено", show_alert=True)
        return

    if action == "ok":
        set_status(booking_id, "confirmed", r["wanted"])
        await bot.send_message(
            r["user_id"],
            f"✅ Ваш запис підтверджено на <b>{e(r['wanted'])}</b>. Чекаємо на вас!",
        )
        await cb.message.edit_text(cb.message.html_text + "\n\n✅ <b>Підтверджено</b>", reply_markup=None)

    elif action == "alt":
        await state.set_state(Propose.waiting_time)
        await state.update_data(booking_id=booking_id)
        await cb.message.answer(
            f"Напишіть, який час запропонувати клієнту (запит #{booking_id}), "
            "напр.: «післязавтра о 15:00».\nСкасувати: /cancel"
        )

    elif action == "no":
        set_status(booking_id, "declined")
        await bot.send_message(
            r["user_id"],
            "На жаль, на бажаний час ми не можемо вас прийняти. "
            "Ми зв'яжемося з вами, щоб підібрати інший варіант, або ви можете залишити новий запит.",
            reply_markup=main_kb(),
        )
        await cb.message.edit_text(cb.message.html_text + "\n\n❌ <b>Відхилено</b>", reply_markup=None)

    await cb.answer()


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Скасовано.", reply_markup=main_kb())


@router.message(Propose.waiting_time, F.text)
async def admin_proposes(m: Message, state: FSMContext, bot: Bot):
    if not is_admin(m.from_user.id):
        return
    data = await state.get_data()
    booking_id = data["booking_id"]
    r = get_booking(booking_id)
    new_time = m.text.strip()[:100]
    set_status(booking_id, "proposed", new_time)
    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Підходить", callback_data=f"cli:ok:{booking_id}")
    kb.button(text="❌ Не підходить", callback_data=f"cli:no:{booking_id}")
    kb.adjust(2)
    await bot.send_message(
        r["user_id"],
        f"🕒 Ми можемо прийняти вас: <b>{e(new_time)}</b>.\nЧи підходить вам цей час?",
        reply_markup=kb.as_markup(),
    )
    await m.answer(f"Запропоновано клієнту (запит #{booking_id}): {e(new_time)}")


# ------------------------------------------------------- клієнт: відповідь
@router.callback_query(F.data.startswith("cli:"))
async def client_reply(cb: CallbackQuery, bot: Bot):
    _, action, raw_id = cb.data.split(":")
    booking_id = int(raw_id)
    r = get_booking(booking_id)
    if not r or r["user_id"] != cb.from_user.id:
        await cb.answer("Запис не знайдено", show_alert=True)
        return

    if action == "ok":
        set_status(booking_id, "confirmed")
        await cb.message.edit_text(f"✅ Домовились! Чекаємо на вас: <b>{e(r['final_time'])}</b>.")
        await notify_admins(bot, f"✅ Клієнт підтвердив час для запиту #{booking_id}: <b>{e(r['final_time'])}</b>\n👤 {e(r['name'])}, 📞 {e(r['phone'])}")
    else:
        set_status(booking_id, "new")
        await cb.message.edit_text("Зрозуміло. Ми зв'яжемося з вами, щоб підібрати інший час.")
        await notify_admins(bot, f"⚠️ Клієнту не підійшов запропонований час (запит #{booking_id}).", admin_kb(booking_id))
    await cb.answer()


# ------------------------------------------------------------ адмін: список
@router.message(Command("list"))
async def list_pending(m: Message):
    if not is_admin(m.from_user.id):
        return
    with db() as c:
        rows = c.execute("SELECT * FROM bookings WHERE status IN ('new','proposed') ORDER BY id").fetchall()
    if not rows:
        await m.answer("Нових запитів немає 🎉")
        return
    for r in rows:
        extra = f"\n🕒 Запропоновано: {e(r['final_time'])}" if r["status"] == "proposed" else ""
        await m.answer(booking_text(r) + extra, reply_markup=admin_kb(r["id"]))


@router.message(Command("confirmed"))
async def list_confirmed(m: Message):
    if not is_admin(m.from_user.id):
        return
    with db() as c:
        rows = c.execute("SELECT * FROM bookings WHERE status = 'confirmed' ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        await m.answer("Підтверджених записів поки немає.")
        return
    await m.answer("\n".join(f"#{r['id']} {e(r['final_time'])} — {e(r['name'])}, {e(r['phone'])}, {e(r['service'])}" for r in rows))


# ---------------------------------------------------------------------- Flask Keep-Alive для Render
app = Flask('')

@app.route('/')
def home():
    return "Bot is active and running!"

def run_bot():
    asyncio.run(main())

# ---------------------------------------------------------------------- запуск
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS не заданий — адміністратор не отримуватиме запити!")
    init_db()
    
    # Запускаємо телеграм-бота у фоновому потоці
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Flask запускаємо на головному потоці — він миттєво відкриє порт для Render
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
