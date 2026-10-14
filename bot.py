"""
Телеграм-бот для запису клієнтів на прийом (з Google-таблицею).

Потік:
1. Клієнт: ім'я -> телефон -> послуга -> марка авто -> номерний знак ->
   бажаний день -> бажаний час -> коментар.
2. Бот каже клієнту, що точний час повідомлять додатково.
3. Адміністратору приходить запит з кнопками:
   ✅ Підтвердити як є | 🕒 Запропонувати інший час | ❌ Відхилити
4. Кожен запит і кожна зміна статусу записується в Google-таблицю
   (якщо задано SHEET_URL).
"""
import asyncio
import html
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
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
# Якщо змінна ADMIN_IDS не задана в Render, запити все одно підуть власнику (ID можна змінити/доповнити змінною)
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "531685798").split(",") if x.strip()}
DB_PATH = os.getenv("DB_PATH", "bookings.db")
# Адреса веб-застосунку Google Apps Script і спільний пароль (необов'язково)
SHEET_URL = os.getenv("SHEET_URL", "")
SHEET_SECRET = os.getenv("SHEET_SECRET", "")
# ID закритої групи (від'ємне число), куди приходять запити. Якщо не задано, запити йдуть адміністраторам особисто
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "-1004402579731") or 0)

# Часовий пояс (сервер Render живе за UTC, тому вказуємо Київ) і мінімальний запас до візиту
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))
MIN_LEAD_MINUTES = int(os.getenv("MIN_LEAD_MINUTES", "60"))

# Відредагуйте під себе
SERVICES = ["Хімчистка", "Поліровка", "Оптика"]
TIME_SLOTS = ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00"]
DAYS_AHEAD = 7
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]

STATUS_UA = {
    "new": "Новий запит",
    "proposed": "Запропоновано інший час",
    "confirmed": "Підтверджено",
    "declined": "Відхилено",
}

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
                car TEXT, plate TEXT,
                wanted TEXT, wanted_plain TEXT, comment TEXT,
                status TEXT DEFAULT 'new',
                final_time TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        # міграція зі старої версії бази (без марки авто і номера)
        cols = {row["name"] for row in c.execute("PRAGMA table_info(bookings)")}
        for col in ("car", "plate", "wanted_plain"):
            if col not in cols:
                c.execute(f"ALTER TABLE bookings ADD COLUMN {col} TEXT")


def get_booking(booking_id: int):
    with db() as c:
        return c.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()


def set_status(booking_id: int, status: str, final_time: str | None = None) -> None:
    with db() as c:
        c.execute(
            "UPDATE bookings SET status = ?, final_time = COALESCE(?, final_time) WHERE id = ?",
            (status, final_time, booking_id),
        )


# ------------------------------------------------------------ Google-таблиця
async def push_sheet(booking_id: int) -> None:
    """Надсилає (або оновлює) рядок запису в Google-таблиці."""
    if not SHEET_URL:
        return
    r = get_booking(booking_id)
    if not r:
        return
    visit_time = r["final_time"] or r["wanted_plain"] or r["wanted"]
    payload = {
        "secret": SHEET_SECRET,
        "id": r["id"],
        "name": r["name"] or "",
        "phone": r["phone"] or "",
        "time": visit_time or "",
        "service": r["service"] or "",
        "car": r["car"] or "Невідома",
        "plate": "",
        "comment": r["comment"] or "",
        "status": STATUS_UA.get(r["status"], r["status"]),
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SHEET_URL, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                await resp.read()
    except Exception as exc:
        logging.warning("Не вдалося записати в Google-таблицю: %s", exc)


async def update_status(booking_id: int, status: str, final_time: str | None = None) -> None:
    set_status(booking_id, status, final_time)
    if status == "confirmed":  # у таблицю йдуть лише затверджені замовлення
        await push_sheet(booking_id)


# --------------------------------------------------------------------- стани
class Booking(StatesGroup):
    name = State()
    phone = State()
    service = State()
    car = State()
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
        f"🚗 {e(r['car'] or 'невідома')}\n"
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


def now_local() -> datetime:
    return datetime.now(TZ)


def day_label(d: date) -> str:
    prefix = {0: "Сьогодні", 1: "Завтра"}.get((d - now_local().date()).days)
    base = f"{d.strftime('%d.%m')} ({WEEKDAYS[d.weekday()]})"
    return f"{prefix}, {base}" if prefix else base


async def notify_admins(bot: Bot, text: str, reply_markup=None) -> None:
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(ADMIN_CHAT_ID, text, reply_markup=reply_markup)
            return
        except Exception as exc:
            logging.warning("Не вдалося надіслати в групу %s: %s. Надсилаю адміністраторам особисто", ADMIN_CHAT_ID, exc)
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


def skip_kb(data: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="Не знаю / пропустити", callback_data=data)
    return kb.as_markup()


async def ask_day(message: Message, state: FSMContext):
    await state.set_state(Booking.day)
    kb = InlineKeyboardBuilder()
    today = now_local().date()
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        kb.button(text=day_label(d), callback_data=f"day:{d.isoformat()}")
    kb.adjust(2)
    await message.answer("Оберіть бажаний день:", reply_markup=kb.as_markup())


@router.callback_query(Booking.service, F.data.startswith("svc:"))
async def got_service(cb: CallbackQuery, state: FSMContext):
    service = SERVICES[int(cb.data.split(":")[1])]
    await state.update_data(service=service)
    await state.set_state(Booking.car)
    await cb.message.edit_text(f"Послуга: <b>{e(service)}</b>")
    await cb.message.answer(
        "Яка марка і модель вашого авто? (напр. Skoda Octavia)\nЯкщо не знаєте, натисніть «Пропустити».",
        reply_markup=skip_kb("skip_car"),
    )
    await cb.answer()


@router.message(Booking.car, F.text)
async def got_car(m: Message, state: FSMContext):
    await state.update_data(car=m.text.strip()[:60])
    await ask_day(m, state)


@router.callback_query(Booking.car, F.data == "skip_car")
async def skip_car(cb: CallbackQuery, state: FSMContext):
    await state.update_data(car="Невідома")
    await cb.message.edit_reply_markup(reply_markup=None)
    await ask_day(cb.message, state)
    await cb.answer()


@router.callback_query(Booking.day, F.data.startswith("day:"))
async def got_day(cb: CallbackQuery, state: FSMContext):
    d = date.fromisoformat(cb.data.split(":", 1)[1])
    earliest = now_local() + timedelta(minutes=MIN_LEAD_MINUTES)
    slots = []
    for t in TIME_SLOTS:
        hh, mm = map(int, t.split(":"))
        if datetime.combine(d, dtime(hh, mm), tzinfo=TZ) >= earliest:
            slots.append(t)
    if not slots:
        await cb.answer("На цей день вільних годин уже немає. Оберіть інший день.", show_alert=True)
        return
    await state.update_data(day=day_label(d), day_iso=d.isoformat())
    await state.set_state(Booking.time)
    kb = InlineKeyboardBuilder()
    for t in slots:
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
        "Додайте коментар (побажання тощо) або натисніть «Пропустити».",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


async def finalize(bot: Bot, chat_id: int, user_id: int, state: FSMContext, comment: str | None):
    data = await state.get_data()
    wanted = f"{data['day']}, {data['time']}"
    visit_date = date.fromisoformat(data["day_iso"])
    wanted_plain = f"{visit_date.strftime('%d.%m.%Y')} ({WEEKDAYS[visit_date.weekday()]}), {data['time']}"
    with db() as c:
        cur = c.execute(
            "INSERT INTO bookings (user_id, name, phone, service, car, wanted, wanted_plain, comment) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, data["name"], data["phone"], data["service"], data.get("car") or "Невідома",
             wanted, wanted_plain, comment),
        )
        booking_id = cur.lastrowid
    await state.clear()

    await bot.send_message(
        chat_id,
        f"✅ Дякуємо, {e(data['name'])}! Ваш запит прийнято.\n\n"
        f"🛠 {e(data['service'])}\n🚗 {e(data['car'] or 'Невідома')}\n"
        f"🗓 Бажаний час: <b>{e(wanted)}</b>\n\n"
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
        await update_status(booking_id, "confirmed", r["wanted_plain"] or r["wanted"])
        await bot.send_message(
            r["user_id"],
            f"✅ Ваш запис підтверджено на <b>{e(r['wanted'])}</b>. Чекаємо на вас!",
        )
        await cb.message.edit_text(cb.message.html_text + "\n\n✅ <b>Підтверджено</b>", reply_markup=None)

    elif action == "alt":
        await state.set_state(Propose.waiting_time)
        await state.update_data(booking_id=booking_id)
        await cb.message.answer(
            f"Натисніть «Відповісти» (Reply) на це повідомлення і напишіть, який час запропонувати клієнту "
            f"(запит #{booking_id}), напр.: «післязавтра о 15:00».\nСкасувати: /cancel"
        )

    elif action == "no":
        await update_status(booking_id, "declined")
        await bot.send_message(
            r["user_id"],
            "Вибачте, на цей час ми, на жаль, не можемо вас прийняти. 🙏\n"
            "Будь ласка, оберіть інший зручний для вас час: натисніть «📝 Записатися» нижче, "
            "і ми з радістю вас запишемо.",
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
    await update_status(booking_id, "proposed", new_time)
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
        await update_status(booking_id, "confirmed")
        await cb.message.edit_text(f"✅ Домовились! Чекаємо на вас: <b>{e(r['final_time'])}</b>.")
        await notify_admins(
            bot,
            f"✅ Клієнт підтвердив час для запиту #{booking_id}: <b>{e(r['final_time'])}</b>\n"
            f"👤 {e(r['name'])}, 📞 {e(r['phone'])}",
        )
    else:
        await update_status(booking_id, "new")
        await cb.message.edit_text("Зрозуміло, дякуємо за відповідь. 🙏")
        await bot.send_message(
            r["user_id"],
            "Будь ласка, оберіть інший зручний для вас час: натисніть «📝 Записатися» нижче.",
            reply_markup=main_kb(),
        )
        await notify_admins(
            bot,
            f"⚠️ Клієнту не підійшов запропонований час (запит #{booking_id}).",
            admin_kb(booking_id),
        )
    await cb.answer()


@router.message(Command("chatid"))
async def chat_id(m: Message):
    await m.answer(f"ID цього чату: <code>{m.chat.id}</code>")


@router.message(Command("myid"))
async def my_id(m: Message):
    status = "ви в списку адміністраторів ✅" if is_admin(m.from_user.id) else "вас НЕМАЄ в списку адміністраторів ❌"
    await m.answer(f"Ваш ID: <code>{m.from_user.id}</code>\n{status}")


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
    await m.answer(
        "\n".join(
            f"#{r['id']} {e(r['final_time'])} — {e(r['name'])}, {e(r['phone'])}, "
            f"{e(r['service'])}, {e(r['car'])}"
            for r in rows
        )
    )


# ---------------------------------------------------------------------- запуск
async def start_web_server() -> None:
    """Маленький веб-сервер: Render (веб-сервіс) вимагає відкритий порт,
    а зовнішній пінг цієї адреси не дає безкоштовному серверу заснути."""
    port = os.getenv("PORT")
    if not port:
        return

    async def health(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    logging.info("Веб-сервер запущено на порту %s", port)


async def main():
    logging.basicConfig(level=logging.INFO)
    logging.info("Адміністратори (ADMIN_IDS): %s", sorted(ADMIN_IDS))
    logging.info("Чат для запитів (ADMIN_CHAT_ID): %s", ADMIN_CHAT_ID or "не задано, запити йдуть адміністраторам особисто")
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS не заданий — адміністратор не отримуватиме запити!")
    if not SHEET_URL:
        logging.info("SHEET_URL не заданий — запис у Google-таблицю вимкнено.")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
