import logging
import asyncio
import caldav
import calendar
import re
from icalendar import Calendar
from icalendar import Calendar as ICalCalendar, Event as ICalEvent
from datetime import datetime, time, timedelta
import pytz
import locale
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler,MessageHandler,filters
from telegram.helpers import escape_markdown
import json
import os
from faq import get_faq_handler, get_faq_callback_handler, get_faq_menu_handler
from eyelash_secret_easteregg import setup_secret_easteregg
from dateutil.relativedelta import relativedelta
from datetime import date as _date
from cryptography.fernet import Fernet

# Загрузка конфигурации
CONFIG_FILE = "config.json"
if not os.path.exists(CONFIG_FILE):
    raise FileNotFoundError(f"Конфигурационный файл {CONFIG_FILE} не найден")

with open(CONFIG_FILE, "r") as file:
    config = json.load(file)
    
# Настройки из config.json
TOKEN = config.get("telegram_token")
CALDAV_URL = config.get("caldav_url")
USERNAME = config.get("caldav_username")
PASSWORD = config.get("caldav_password")
PRICE_URL = config.get("price_url")
CALENDAR_NAME = config.get("calendar_name", "Work")
LOG_FILE = config.get("log_file", "bot.log")
USERS_FILE = config.get("users_file", "users.json")
ADMIN_IDS = config.get("admin_ids")
ADMIN_ID = config.get("admin_id")
PHONE = config.get("phone")
WAITLIST_FILE = config.get("waitlist_file", "waitlist.json")
OPEN_MONTHS_FILE = config.get("open_months")
BOOKINGS_FILE = config.get("bookings")
PROFILES_FILE = config.get("profiles")
ASK_FIRST_NAME, ASK_LAST_NAME, ASK_PHONE = range(3)
EDIT_USER_ID, EDIT_FIELD_CHOICE, EDIT_FIELD_INPUT = range(3)
FERNET_KEY = config.get("fernet_key")
INSTAGRAM_URL = config.get("instagram_url", "https://instagram.com/")
NOTICE_FILE = config.get("notice_file", "notice.txt")
NOTICE_STATE = 990    # уникальный int, не пересекается с другими state'ами

# Настройки логирования
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        #logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
TZ = pytz.timezone("Europe/Moscow")
locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')
pending_bookings = {}  # должно быть глобально
from git import Repo
import shutil

import uuid

pending_bookings = {}  # booking_id → {user_id, name, date, slot}
if not FERNET_KEY:
    raise RuntimeError("В config.json должен быть указан fernet_key")
FERNET = Fernet(FERNET_KEY.encode())
def encrypt_bytes(b: bytes) -> bytes:
    return FERNET.encrypt(b)

def decrypt_bytes(b: bytes) -> bytes:
    return FERNET.decrypt(b)

def load_notice() -> str:
    return open(NOTICE_FILE, "r", encoding="utf‑8").read().strip() if os.path.exists(NOTICE_FILE) else ""

def save_notice(text: str):
    with open(NOTICE_FILE, "w", encoding="utf‑8") as f:
        f.write(text.strip())


# ———————— Нормализация телефона ————————

def normalize_phone(raw: str) -> str | None:
    """
    Оставляет только цифры, а затем:
     - если 10 цифр: добавляет '7' в начало;
     - если 11 цифр и начинается с '8': меняет '8' на '7';
     - если 11 цифр и начинается с '7': оставляет;
    В конце возвращает строку '+7XXXXXXXXXX' или None, если невалидно.
    """
    digits = re.sub(r'\D', '', raw)
    # 10 цифр → локальный номер
    if re.fullmatch(r'\d{10}', digits):
        digits = '7' + digits
    # 11 цифр, начинается с 8 → заменяем на 7
    elif re.fullmatch(r'8\d{10}', digits):
        digits = '7' + digits[1:]
    # 11 цифр, начиная с 7 → корректно
    elif re.fullmatch(r'7\d{10}', digits):
        pass
    else:
        return None
    return '+' + digits
# ————————————————————————————————————————

def load_waitlist() -> dict:
    if os.path.exists(WAITLIST_FILE):
        with open(WAITLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_waitlist(waitlist: dict):
    with open(WAITLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(waitlist, f, ensure_ascii=False, indent=2)

async def view_waitlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    waitlist = load_waitlist()
    # ищем все даты, где пользователь есть в очереди
    entries = [key for key, lst in waitlist.items() if user_id in lst]

    if not entries:
        await query.edit_message_text("📭 Вы ни в одной очереди ожидания не состоите.", reply_markup=get_main_menu(int(user_id)))
        return

    # строим список кнопок — одна кнопка на каждую дату
    keyboard = []
    for key in sorted(entries):
        # key формат "YYYY-MM-DD" → отображаем "DD.MM.YYYY"
        y, m, d = key.split("-")
        date_str = f"{d}.{m}.{y}"
        keyboard.append([
            InlineKeyboardButton(f"{date_str} ❌ Отменить", callback_data=f"cancel_wait_{key}")
        ])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="user_bookings")])

    text = "⏳ *Ваши листы ожидания:*\n\n" + "\n".join(f"• {d}.{m}.{y}" for y,m,d in (e.split("-") for e in entries))
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def cancel_waitlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # query.data == "cancel_wait_YYYY-MM-DD"
    _, _, key = query.data.partition("cancel_wait_")
    user_id = str(query.from_user.id)

    waitlist = load_waitlist()
    lst = waitlist.get(key, [])
    if user_id not in lst:
        await query.edit_message_text("❌ Вы уже не в этой очереди.", reply_markup=get_main_menu(int(user_id)))
        return

    lst.remove(user_id)
    if lst:
        waitlist[key] = lst
    else:
        del waitlist[key]
    save_waitlist(waitlist)

    # подтверждаем юзеру
    y, m, d = key.split("-")
    date_str = f"{d}.{m}.{y}"
    await query.edit_message_text(f"✅ Вы удалены из листа ожидания на {date_str}.", reply_markup=get_main_menu(int(user_id)))


def load_profiles():
    if not os.path.exists(PROFILES_FILE):
        return {}
    with open(PROFILES_FILE, "rb") as f:
        encrypted = f.read()
    try:
        decrypted = decrypt_bytes(encrypted)
        return json.loads(decrypted.decode("utf-8"))
    except Exception as e:
        logger.error("Не удалось расшифровать profiles: %s", e)
        return {}

def save_profiles(profiles):
    data = json.dumps(profiles, ensure_ascii=False).encode("utf-8")
    encrypted = encrypt_bytes(data)
    with open(PROFILES_FILE, "wb") as f:
        f.write(encrypted)


def load_bookings():
    if os.path.exists(BOOKINGS_FILE):
        with open(BOOKINGS_FILE, "r") as f:
            return json.load(f)
    return []

def save_bookings(bookings):
    with open(BOOKINGS_FILE, "w") as f:
        json.dump(bookings, f, indent=4)



def load_open_months():
    if not os.path.exists(OPEN_MONTHS_FILE):
        return []
    with open(OPEN_MONTHS_FILE, "r") as f:
        return json.load(f)

def save_open_months(open_months):
    with open(OPEN_MONTHS_FILE, "w") as f:
        json.dump(open_months, f, indent=4)


def get_closed_months(n=6):
    now = datetime.now(TZ)
    open_months = set(load_open_months())
    closed = []

    for i in range(n):
        dt = (now.replace(day=1) + timedelta(days=32 * i)).replace(day=1)
        key = dt.strftime("%Y-%m")
        if key not in open_months:
            closed.append((key, dt.strftime("%B %Y")))
    return closed

async def admin_edit_user_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    if int(user_id) not in ADMIN_IDS:
        await query.edit_message_text("⛔ У вас нет прав.")
        return ConversationHandler.END

    # Список всех профилей
    profiles = load_profiles()
    keyboard = []

    for uid, profile in profiles.items():
        label = f"{profile.get('first_name', 'неизвестно')} {profile.get('last_name', 'неизвестно')} ({uid})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"edit_user_{uid}")])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit_user")])
    await query.edit_message_text("👤 Выберите пользователя для редактирования:", reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_USER_ID


async def admin_select_user_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_edit_user":
        await query.edit_message_text("❌ Редактирование отменено.")
        return ConversationHandler.END

    user_id = query.data.replace("edit_user_", "")
    context.user_data["edit_user_id"] = user_id

    keyboard = [
        [InlineKeyboardButton("✏️ Имя", callback_data="edit_field_first_name")],
        [InlineKeyboardButton("✏️ Фамилия", callback_data="edit_field_last_name")],
        [InlineKeyboardButton("📱 Телефон", callback_data="edit_field_phone")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit_user")],
    ]
    await query.edit_message_text("🔧 Выберите поле для редактирования:", reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_FIELD_CHOICE


async def admin_input_user_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action = query.data.replace("edit_field_", "")
    context.user_data["edit_field"] = action

    await query.edit_message_text(f"Введите новое значение для поля: {action}")
    return EDIT_FIELD_INPUT


async def admin_save_user_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    user_id = context.user_data.get("edit_user_id")
    field = context.user_data.get("edit_field")

    if not user_id or not field:
        await update.message.reply_text("⚠️ Ошибка: не выбраны пользователь или поле.")
        return ConversationHandler.END

    profiles = load_profiles()
    if user_id not in profiles:
        await update.message.reply_text("⚠️ Пользователь не найден.")
        return ConversationHandler.END

    profiles[user_id][field] = user_input
    save_profiles(profiles)
    logger.info(f"[Admin] Обновлён профиль {user_id}: {field} = {user_input}")
    await update.message.reply_text("✅ Данные обновлены успешно.")
    return ConversationHandler.END

async def user_cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    booking_id = query.data.replace("user_cancel_", "")
    bookings = load_bookings()

    for b in bookings:
        if b["id"] == booking_id and b["user_id"] == user_id:
            if b["status"] in ("pending", "confirmed"):
                b["status"] = "cancelled"
                save_bookings(bookings)
                pending_bookings.pop(booking_id, None)
                # — уведомляем первому в листе ожидания на эту дату —
                key = datetime.strptime(b["date"], "%d.%m.%Y").strftime("%Y-%m-%d")
                waitlist = load_waitlist()
                queue = waitlist.get(key, [])
                if queue:
                    next_user = queue.pop(0)
                    if queue:
                        waitlist[key] = queue
                    else:
                        del waitlist[key]
                    save_waitlist(waitlist)

                    await context.bot.send_message(
                        chat_id=int(next_user),
                        text=(
                            f"🔔 На {b['date']} освободился слот! "
                            "Нажмите, чтобы выбрать время:"
                        ),
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("📅 Выбрать время", callback_data="calendar_open")
                        ]])
                    )

                # 2) Удаляем событие из календаря
                day, month, year = map(int, b["date"].split("."))
                hour, minute    = map(int, b["slot"].split(":"))
                # собираем summary так же, как при создании
                prof = load_profiles().get(str(user_id), {})
                summary = f"{prof.get('phone','')} {prof.get('first_name','')} {prof.get('last_name','')}"
                deleted = delete_event(year, month, day, hour, minute, summary)
                if not deleted:
                    logger.warning(f"[Cancel] Событие не найдено/не удалено: {summary} at {day}.{month}.{year} {hour:02d}:{minute:02d}")

                await query.edit_message_text("✅ Ваша запись успешно отменена.")

                # Уведомление админу (опционально)
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🚫 Пользователь *{b['name']}* отменил запись:\n📅 {b['date']} в {b['slot']}",
                    parse_mode="Markdown"
                )
                return

    await query.edit_message_text("❌ Невозможно отменить эту запись.")

async def admin_free_slots_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if user_id not in ADMIN_IDS:
        await query.edit_message_text("⛔ У вас нет прав.")
        return

    today = datetime.now(TZ).date()
    current_key = f"{today.year}-{today.month:02d}"
    open_months = sorted(load_open_months())

    # Список месяцев: текущий + первый следующий открытый
    months = [current_key]
    for key in open_months:
        if key > current_key:
            months.append(key)
            break

    cal = IrCalendar()
    blocks = []

    for key in months:
        year, month = map(int, key.split("-"))
        month_name = datetime(year, month, 1).strftime("%B %Y")
        free_by_day = await cal.find_free_slots_month(year, month)

        lines = []
        for day, slots in sorted(free_by_day.items()):
            # для текущего месяца пропускаем прошедшие
            if key == current_key and day < today.day:
                continue
            times = ", ".join(dt.strftime("%H:%M") for dt in slots)
            lines.append(f"{day:02d}.{month:02d}: {times}")

        if lines:
            blocks.append(f"*Свободные окна на {month_name}:*\n" + "\n".join(lines))
        else:
            blocks.append(f"❌ В {month_name} нет свободных окон.")

    text = "\n\n".join(blocks)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=get_main_menu(user_id))


async def handle_admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "admin_open_month":
        await open_month_command(update, context)
    elif data == "admin_subscribers":
        await subscribers_count(update, context)


async def admin_month_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    now = datetime.now(TZ)
    month_start = datetime(now.year, now.month, 1).date()
    month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    bookings = load_bookings()
    profiles = load_profiles()

    # Фильтруем заявки по месяцу
    month_bookings = [b for b in bookings if b["status"] == "confirmed"]

    response = f"📅 *Заявки за {month_start.strftime('%B %Y')}*\n\n"
    if not month_bookings:
        response += "❌ Нет подтверждённых заявок."
    else:
        for b in month_bookings:
            user_id = str(b["user_id"])
            profile = profiles.get(user_id, {})
            response += (
                f"👤 {profile.get('first_name', '–')} {profile.get('last_name', '–')}\n"
                f"📱 {profile.get('phone', '–')}\n"
                f"📅 {b['date']} в {b['slot']}\n"
                f"{'-' * 30}\n"
            )

    await query.edit_message_text(response, parse_mode="Markdown", reply_markup=get_main_menu(int(query.from_user.id)))

async def show_user_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    profiles = load_profiles()
    profile = profiles.get(user_id)

    if not profile:
        await query.edit_message_text("📝 Профиль не найден. Вы можете заполнить его при следующей записи.")
        return

    history = profile.get("history", [])
    history_text = "\n".join(history) if history else "—"

    text = (
        f"👤 *Ваш профиль:*\n"
        f"Имя: {profile['first_name']}\n"
        f"Фамилия: {profile['last_name']}\n"
        f"Телефон: {profile['phone']}\n"
        f"🗓 История:\n{history_text}"
    )

    buttons = [
        [InlineKeyboardButton("✏️ Изменить имя", callback_data="edit_first_name")],
        [InlineKeyboardButton("✏️ Изменить фамилию", callback_data="edit_last_name")],
        [InlineKeyboardButton("✏️ Изменить телефон", callback_data="edit_phone")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="calendar_open")]
    ]

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))



async def ask_first_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("➡️ ask_first_name reached")
    raw = update.message.text.strip()
    # если пусто — берём из профиля Telegram
    if not raw:
        raw = update.message.from_user.first_name or ""
    logger.debug(f"[Profile] Received first_name: {raw} (user_id={update.message.from_user.id})")
    context.user_data["first_name"] = raw
    await update.message.reply_text("Теперь введите вашу *фамилию*:")
    return ASK_LAST_NAME

async def ask_last_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("➡️ ask_last_name reached")
    raw = update.message.text.strip()
    # если пусто — берём из профиля Telegram
    if not raw:
        raw = update.message.from_user.last_name or ""
    logger.debug(f"[Profile] Received last_name: {raw} (user_id={update.message.from_user.id})")
    context.user_data["last_name"] = raw
    await update.message.reply_text("📱 Введите номер телефона (пример: +79001234567):")
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("➡️ ask_phone reached")
    phone = update.message.text.strip()
    user_id = str(update.message.from_user.id)

    logger.debug(f"[Profile] Received phone: {phone} (user_id={user_id})")

    # Валидация
    if not phone.startswith("+7") or not phone[1:].isdigit() or len(phone) != 12:
        logger.warning(f"[Profile] Invalid phone format: {phone}")
        await update.message.reply_text(
            "❌ Неверный формат. Введите номер в формате +79001234567:"
        )
        return ASK_PHONE

    try:
        # Сохраняем профиль
        profiles = load_profiles()
        profiles[user_id] = {
            "first_name": context.user_data.get("first_name", "неизвестно"),
            "last_name":  context.user_data.get("last_name",  "неизвестно"),
            "phone":      phone or "неизвестно",
            "history":    []
        }

        # Добавляем запись в историю профиля, если она есть
        booking_id = context.user_data.get("confirm_booking_id")
        bookings = load_bookings()
        for b in bookings:
            if b["id"] == booking_id:
                profiles[user_id]["history"].append(f"{b['date']} {b['slot']}")
                break

        save_profiles(profiles)
        logger.info(f"[Profile] Профиль сохранён для user_id={user_id}: {profiles[user_id]}")

        # Уведомляем пользователя
        await update.message.reply_text(
            "✅ Профиль сохранён. Спасибо!",
            reply_markup=get_main_menu(int(user_id))
        )

        # —————————————
        # Отправляем администратору заявку на подтверждение
        booking = pending_bookings.get(booking_id)
        if booking:
            buttons = [[
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_{booking_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{booking_id}")
            ]]
            admin_text = (
                f"📬 *Новая заявка*\n"
                f"👤 [{booking['name']}](tg://user?id={booking['user_id']})\n"
                f"📅 *Дата:* {booking['date']}\n"
                f"🕒 *Время:* {booking['slot']}"
            )
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        # —————————————

        # Завершаем ConversationHandler
        return ConversationHandler.END

    except Exception as e:
        logger.exception(f"[Profile] Ошибка при сохранении профиля: {e}")
        await update.message.reply_text(
            "❌ Произошла ошибка при сохранении профиля. Попробуйте позже."
        )
        return ConversationHandler.END


async def show_user_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    bookings = load_bookings()
    my = [b for b in bookings if b["user_id"] == user_id and b["status"] in ("pending", "confirmed")]

    if not my:
        await query.edit_message_text("📭 У вас нет активных записей.", reply_markup=get_main_menu(user_id))
        return

    for b in my:
        text = f"📅 *{b['date']}* — 🕒 {b['slot']}\nСтатус: `{b['status']}`"
        buttons = [[InlineKeyboardButton("❌ Отменить", callback_data=f"user_cancel_{b['id']}")]]
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    await context.bot.send_message(chat_id=user_id, text="⬇️ Главное меню", reply_markup=get_main_menu(user_id))

async def admin_open_month_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in ADMIN_IDS:
        await query.edit_message_text("⛔ У вас нет прав.")
        return

    key = query.data.replace("admin_open_", "")
    open_months = load_open_months()

    if key not in open_months:
        open_months.append(key)
        save_open_months(open_months)
        await query.edit_message_text(f"✅ Месяц *{key}* открыт для записи.", parse_mode="Markdown")
    else:
        await query.edit_message_text(f"ℹ️ Месяц *{key}* уже был открыт.", parse_mode="Markdown")

async def open_month_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if user_id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ У вас нет прав.")
        return

    closed_months = get_closed_months()
    if not closed_months:
        await update.effective_message.reply_text("✅ Все ближайшие месяцы уже открыты.")
        return

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"admin_open_{key}")]
        for key, name in closed_months
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.effective_message.reply_text("🔓 Выберите месяц для открытия:", reply_markup=reply_markup)


def upload_price_to_github():
    repo_url = config.get("github_repo_url")
    branch = config.get("github_branch", "main")
    local_path = config.get("github_local_path", "repo_clone")
    remote_dir = config.get("github_remote_dir", ".")

    if not all([repo_url, branch, local_path, remote_dir]):
        logger.error("❌ Не указаны параметры GitHub в config.json")
        return

    # Удаляем старый клон (если был)
    if os.path.exists(local_path):
        shutil.rmtree(local_path)

    try:
        logger.info("📦 Клонируем репозиторий GitHub...")
        repo = Repo.clone_from(repo_url, local_path, branch=branch)

        # Копируем price.html
        dst_path = os.path.join(local_path, remote_dir)
        os.makedirs(dst_path, exist_ok=True)
        shutil.copy("price.html", os.path.join(dst_path, "price.html"))

        # Git: add, commit, push
        repo.git.add(A=True)
        repo.index.commit("🔄 Обновление прайса из Telegram-бота")
        repo.remote().push()
        logger.info("✅ price.html успешно загружен в GitHub!")

    except Exception as e:
        logger.error(f"❌ Ошибка при выгрузке в GitHub: {e}")
        
class IrCalendar:
    def __init__(self):
        self.caldav_url = CALDAV_URL
        self.username = USERNAME
        self.password = PASSWORD
        logger.info("IrCalendar initialized with CalDAV URL: %s", self.caldav_url)

    def parse_datetime(self, dt_obj):
        """Парсит объект даты и делает его timezone-aware."""
        logger.debug("Parsing datetime: %s", dt_obj)
        if isinstance(dt_obj, datetime):
            return dt_obj if dt_obj.tzinfo else TZ.localize(dt_obj)
        return None

    def get_busy_slots_sync(self, selected_date):
        """Получает занятые слоты синхронно на указанную дату из CalDAV."""
        logger.info("Getting busy slots for date: %s", selected_date)
        try:
            client = caldav.DAVClient(url=self.caldav_url, username=self.username, password=self.password)
            my_principal = client.principal()
            calendars = my_principal.calendars()

            if not calendars:
                logger.warning("No calendars found.")
                return []

            for c in calendars:
                if c.name == CALENDAR_NAME:  # Предполагаем, что нужный календарь называется 'Work'
                    calendar = client.calendar(url=c.url)
                    events = calendar.date_search(selected_date)

                    busy_slots = []
                    for event in events:
                        gcal = Calendar.from_ical(event.data)
                        for component in gcal.walk():
                            if component.name == "VEVENT":
                                dtstart = self.parse_datetime(component.get('dtstart').dt)
                                dtend = self.parse_datetime(component.get('dtend').dt)
                                if dtstart and dtend:
                                    busy_slots.append((dtstart, dtend))
                    logger.info("Found busy slots: %s", busy_slots)
                    return busy_slots
        except Exception as e:
            logger.error("Error while getting calendar events: %s", e)
            return []

    async def get_busy_slots(self, selected_date):
        """Асинхронно вызывает синхронный метод."""
        logger.debug("Asynchronously getting busy slots for date: %s", selected_date)
        loop = asyncio.get_event_loop()
        busy_slots = await loop.run_in_executor(None, self.get_busy_slots_sync, selected_date)
        return busy_slots

    async def find_free_slots_async(self, selected_date):
        """Асинхронно находит свободные слоты с шагом 3 часа."""
        logger.info("Finding free slots for date: %s", selected_date)
        work_start_dt = TZ.localize(datetime.combine(selected_date, time(10, 0)))
        work_end_dt = TZ.localize(datetime.combine(selected_date, time(22, 0)))

        busy_slots = await self.get_busy_slots(selected_date)  # Асинхронный вызов
        free_slots = []

        current_start = work_start_dt
        while current_start < work_end_dt:
            current_end = current_start + timedelta(hours=3)
            is_busy = any(start < current_end and end > current_start for start, end in busy_slots)

            if not is_busy:
                free_slots.append(current_start)

            current_start = current_end

        logger.info("Found free slots: %s", free_slots)
        return free_slots

    def _get_month_events_sync(self, start_date: _date, end_date: _date):
        """
        Синхронно получает все события из CalDAV между start_date и end_date.
        Возвращает список (dtstart, dtend).
        """
        try:
            client = caldav.DAVClient(url=self.caldav_url, username=self.username, password=self.password)
            principal = client.principal()
            calendars = principal.calendars()
            if not calendars:
                return []
            for c in calendars:
                if c.name == CALENDAR_NAME:
                    calendar_obj = client.calendar(url=c.url)
                    events = calendar_obj.date_search(start_date, end_date)
                    busy = []
                    for ev in events:
                        gcal = Calendar.from_ical(ev.data)
                        for comp in gcal.walk():
                            if comp.name == "VEVENT":
                                dtstart = self.parse_datetime(comp.get("dtstart").dt)
                                dtend   = self.parse_datetime(comp.get("dtend").dt)
                                if dtstart and dtend:
                                    busy.append((dtstart, dtend))
                    return busy
        except Exception as e:
            logger.error(f"Error fetching month events: {e}")
        return []

    async def get_month_events(self, year: int, month: int):
        """
        Асинхронно получает все события за year/month (с 1-го до 1-го следующего месяца).
        """
        start = _date(year, month, 1)
        end   = (start + relativedelta(months=1))
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self._get_month_events_sync,
            start,
            end
        )

    async def find_free_slots_month(self, year: int, month: int):
        """
        Находит свободные 3-часовые окна в диапазоне 10:00–22:00
        для всех дней year/month за один запрос.
        Возвращает dict: {день: [datetime1, datetime2, ...], ...}.
        """
        busy_events = await self.get_month_events(year, month)
        # Группируем по дню
        busy_by_day = {}
        for start, end in busy_events:
            d = start.date().day
            busy_by_day.setdefault(d, []).append((start, end))

        free_by_day = {}
        last_day = calendar.monthrange(year, month)[1]
        for day in range(1, last_day + 1):
            work_start = TZ.localize(datetime.combine(_date(year, month, day), time(10, 0)))
            work_end   = TZ.localize(datetime.combine(_date(year, month, day), time(22, 0)))
            busy_slots = busy_by_day.get(day, [])
            current = work_start
            frees = []
            while current < work_end:
                nxt = current + timedelta(hours=3)
                if not any(bs < nxt and be > current for bs, be in busy_slots):
                    frees.append(current)
                current = nxt
            if frees:
                free_by_day[day] = frees

        return free_by_day

    async def update_calendar_status(self, year: int, month: int):
        """
        Возвращает словарь days_status: ключ — номер дня, значение — 
        '✅' (есть свободные слоты) или '⛔' (нет).
        """
        # 1) Тянем разом все свободные слоты на месяц
        free_by_day = await self.find_free_slots_month(year, month)

        days_status = {}
        today = datetime.now(TZ).date()
        last_day = calendar.monthrange(year, month)[1]

        for day in range(1, last_day + 1):
            # прошедшие дни — ❌
            if datetime(year, month, day).date() < today:
                days_status[day] = "❌"
            # есть свободные — ✅
            elif day in free_by_day:
                days_status[day] = "✅"
            # иначе — ⛔
            else:
                days_status[day] = "⛔"

        return days_status

def connect_calendar():
    """Возвращает объект CalDAV-календаря по имени."""
    client = caldav.DAVClient(url=CALDAV_URL, username=USERNAME, password=PASSWORD)
    principal = client.principal()
    for c in principal.calendars():
        if c.name == CALENDAR_NAME:
            return client.calendar(url=c.url)
    raise Exception(f"Calendar '{CALENDAR_NAME}' not found")

def is_slot_free(year: int, month: int, day: int, hour: int, minute: int, duration_hours: float) -> bool:
    """Проверяет, свободен ли указанный интервал."""
    cal = connect_calendar()
    start_dt = TZ.localize(datetime(year, month, day, hour, minute))
    end_dt   = start_dt + timedelta(hours=duration_hours)
    events = cal.date_search(start_dt.date(), (end_dt + timedelta(days=1)).date())
    for ev in events:
        gcal = ICalCalendar.from_ical(ev.data)
        for comp in gcal.walk():
            if comp.name == "VEVENT":
                ev_start = comp.get("dtstart").dt
                ev_end   = comp.get("dtend").dt
                if isinstance(ev_start, datetime) and not ev_start.tzinfo:
                    ev_start = TZ.localize(ev_start)
                if isinstance(ev_end, datetime) and not ev_end.tzinfo:
                    ev_end = TZ.localize(ev_end)
                # проверяем пересечение
                if ev_start < end_dt and ev_end > start_dt:
                    return False
    return True

def create_event(year: int, month: int, day: int, hour: int, minute: int,
                 duration_hours: float, summary: str) -> bool:
    """
    Создаёт событие в календаре, если слот свободен.
    summary — строка «телефон Имя Фамилия».
    """
    if not is_slot_free(year, month, day, hour, minute, duration_hours):
        logger.warning(f"Slot {day}.{month}.{year} {hour:02d}:{minute:02d} occupied")
        return False

    cal = connect_calendar()
    ical = ICalCalendar()
    ical.add("prodid", "-//Telegram Bot//")
    ical.add("version", "2.0")

    ev = ICalEvent()
    start_dt = TZ.localize(datetime(year, month, day, hour, minute))
    end_dt   = start_dt + timedelta(hours=duration_hours)

    ev.add("uid", str(uuid.uuid4()))
    ev.add("dtstamp", datetime.now(TZ))
    ev.add("dtstart", start_dt)
    ev.add("dtend",   end_dt)
    ev.add("summary", summary)

    ical.add_component(ev)
    cal.add_event(ical.to_ical())
    logger.info(f"Created calendar event: {summary} at {start_dt}")
    return True

def delete_event(year: int, month: int, day: int,
                 hour: int, minute: int,
                 summary: str) -> bool:
    """
    Ищет в календаре событие с точным start_dt и summary и удаляет его.
    Возвращает True, если найдено и удалено.
    """
    cal = connect_calendar()
    start_dt = TZ.localize(datetime(year, month, day, hour, minute))
    # ищем все события за этот день
    events = cal.date_search(start_dt.date(), start_dt.date() + timedelta(days=1))
    for ev in events:
        gcal = ICalCalendar.from_ical(ev.data)
        for comp in gcal.walk():
            if comp.name == "VEVENT" and comp.get("summary") == summary:
                ev_start = comp.get("dtstart").dt
                # нормализуем timezone
                if isinstance(ev_start, datetime) and not ev_start.tzinfo:
                    ev_start = TZ.localize(ev_start)
                if ev_start == start_dt:
                    ev.delete()
                    logger.info(f"Deleted event from calendar: {summary} at {start_dt}")
                    return True
    return False


def generate_calendar(year, month, days_status, mode="auto"):
    """
    Создает inline-календарь на указанный месяц, адаптируя под телефон или компьютер.
    mode = "phone" → 7 кнопок в строке (неделя).
    mode = "desktop" → 10 кнопок в строке.
    mode = "auto" → автоопределение.
    """
    logger.debug("Generating calendar for year: %d, month: %d, mode: %s", year, month, mode)
    open_months = load_open_months()
    key = f"{year}-{month:02d}"
    if key not in open_months:
        logger.info("Месяц %s-%s не открыт для записи.", year, month)
        return InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Месяц закрыт", callback_data="none")]])

    first_day = datetime(year, month, 1)
    last_day = (first_day + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    # Дни недели (первый день - понедельник)
    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    # Определяем режим
    if mode == "auto":
        mode = "phone"  # По умолчанию считаем, что у пользователя телефон

    max_buttons_per_row = 7 if mode == "phone" else 10  # 7 для телефона, 10 для ПК

    # Добавляем строку с днями недели
    days_buttons = [[InlineKeyboardButton(day, callback_data="none") for day in week_days]]

    row = []
    start_weekday = first_day.weekday()  # Определяем, с какого дня недели начинается месяц

    # Добавляем пустые ячейки перед первым числом
    for _ in range(start_weekday):
        row.append(InlineKeyboardButton(" ", callback_data="none"))

    today = datetime.now(TZ).date()
    # Заполняем календарь днями с новыми символами
    for day in range(1, last_day.day + 1):
        current_date = datetime(year, month, day).date()
        status = days_status.get(day, "❓")

        # Изменяем отображение дней
        if current_date < today:
            day_text = "❌"
            #day_text = f"{day}"
            callback_data = "none"
        elif status == "✅":
            day_text = f"{day}"
            callback_data = f"day_{year}_{month}_{day}"
        elif status == "⛔":
            day_text = "❌"
            callback_data = f"join_wait_{year}_{month}_{day}"
            #callback_data = "none"
        else:
            day_text = f"{day}"
            callback_data = "none"
        row.append(InlineKeyboardButton(day_text, callback_data=callback_data))

        # Завершаем строку после 7 кнопок (неделя)
        if len(row) == max_buttons_per_row:
            days_buttons.append(row)
            row = []

    # Добавляем последнюю строку, если есть нераспределенные кнопки
    if row:
        while len(row) < max_buttons_per_row:
            row.append(InlineKeyboardButton(" ", callback_data="none"))
        days_buttons.append(row)

    # Кнопки навигации (влево, месяц, вправо)
    navigation_buttons = [
        InlineKeyboardButton("⬅️", callback_data=f"prev_month_{year}_{month}"),
        InlineKeyboardButton(first_day.strftime("%B %Y"), callback_data="none"),
        InlineKeyboardButton("➡️", callback_data=f"next_month_{year}_{month}")
    ]

    days_buttons.append(navigation_buttons)

    logger.debug("Generated calendar buttons: %s", days_buttons)
    return InlineKeyboardMarkup(days_buttons)

# ─── 2.  Callback: админ нажал кнопку ───────────────────────────────────────────
async def admin_notice_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry‑point: показывает администратору форму ввода."""
    query = update.callback_query
    await query.answer()

    await context.bot.send_message(
        chat_id=query.from_user.id,
        text=("✏️ Отправьте текст объявления.\n"
              "Напишите *off* (или 0), чтобы убрать баннер."),
        parse_mode="Markdown"
    )
    return NOTICE_STATE

# ─── 3.  Callback: админ прислал текст ─────────────────────────────────────────
async def admin_notice_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет объявление и даёт подтверждение."""
    text = update.message.text.strip()

    if text.lower() in {"off", "disable", "нет", "0"}:
        save_notice("")
        reply = "❌ Объявление отключено."
    else:
        save_notice(text)
        reply = "✅ Объявление обновлено."

    await update.message.reply_text(reply, reply_markup=get_main_menu(update.effective_user.id))
    return ConversationHandler.END

def get_main_menu(user_id=None):
    """Создает меню с основными кнопками."""
    keyboard = [
        [InlineKeyboardButton("📅 Текущий месяц", callback_data="calendar_open")],
        [InlineKeyboardButton("📸 Instagram", url=INSTAGRAM_URL)],
        [InlineKeyboardButton("📄 Прайс", callback_data="price_html")],  # Новая кнопка
        [InlineKeyboardButton("📞 Контакты", callback_data="contacts_button")],
        [InlineKeyboardButton("🧑‍🎓 FAQ", callback_data="faq_menu")],
        [InlineKeyboardButton("📋 Мои заявки", callback_data="user_bookings"),
         InlineKeyboardButton("⏳ Лист ожидания", callback_data="view_waitlist")],
        [InlineKeyboardButton("📖 Мой профиль", callback_data="user_profile")]
    ]
    if user_id in ADMIN_IDS:
        keyboard.append(
            [InlineKeyboardButton("📅 Заявки на месяц", callback_data="admin_month_bookings")]
        )

        keyboard.append(
            [InlineKeyboardButton("📣 Объявление", callback_data="admin_notice")]
        )

        keyboard.append(
            [InlineKeyboardButton("🕒 Свободные слоты", callback_data="admin_free_slots_month")]
        )
        keyboard.append([
            InlineKeyboardButton("🔓 Открыть месяц", callback_data="admin_open_month")
        ])
        keyboard.append([
            InlineKeyboardButton("👥 Подписчики", callback_data="admin_subscribers")
        ])
        keyboard.append([
            InlineKeyboardButton("🛠 Редактировать профиль", callback_data="admin_edit_profile")
        ])
    return InlineKeyboardMarkup(keyboard)

  
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start. Добавляет пользователя в базу подписчиков."""
    user = update.message.from_user
    user_id = user.id
    user_name = user.full_name
    username = user.username if user.username else "Нет"
    date_subscribed = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Проверяем, есть ли уже пользователь в базе
    if not any(sub["id"] == user_id for sub in subscribers):
        subscribers.append({
            "id": user_id,
            "name": user_name,
            "username": username,
            "date_subscribed": date_subscribed
        })
        save_users(subscribers)
        logger.info(f"Новый подписчик: {user_name} (@{username}, {user_id})")

    # Отправляем пользователю календарь
    logger.info("Start command received.")
    cal = IrCalendar()
    now = datetime.now(TZ)

    days_status = {day: "❓" for day in range(1, 32)}
    reply_markup = generate_calendar(now.year, now.month, days_status)

    combined_keyboard = get_main_menu(user_id).inline_keyboard + reply_markup.inline_keyboard
    full_reply_markup = InlineKeyboardMarkup(combined_keyboard)

    message = await update.message.reply_text("📅 Выберите дату:", reply_markup=full_reply_markup)

    asyncio.create_task(update_calendar_after_sync(message, now.year, now.month, cal))

async def subscribers_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if user_id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ У вас нет прав для выполнения этой команды.")
        return

    count = len(subscribers)
    message = f"📊 *Всего подписчиков: {count}*\n\n"

    if count == 0:
        message += "❌ Нет подписчиков."
    else:
        for sub in subscribers:
            name = escape_markdown(sub['name'], version=2)
            user_id = sub['id']
            date_subscribed = escape_markdown(sub['date_subscribed'], version=2)
            username = escape_markdown(sub['username'], version=2) if sub['username'] else "Без юзернейма"
            username_display = f"🔗 @{username}" if sub['username'] else "🔗 Без юзернейма"

            message += (
                f"👤 *{name}*\n"
                f"\\(ID: `{user_id}`\\)\n"
                f"📅 Подписался: {date_subscribed}\n"
                f"{username_display}\n"
                f"{'\\-' * 30}\n"
            )
    # Кнопки для навигации
    keyboard = [
        [InlineKeyboardButton("📅 Текущий месяц", callback_data="calendar_open")],
        [InlineKeyboardButton("💵 Прайс", callback_data="price_button")],
        [InlineKeyboardButton("📞 Контакты", callback_data="contacts_button")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.effective_message.reply_text(message, parse_mode="MarkdownV2", reply_markup=get_main_menu(user_id))


async def update_calendar_after_sync(message, year, month, cal, user_id=None):
    """Фоновая задача обновления календаря после синхронизации с Yandex Календарем."""

    loading_message = await message.reply_text("⏳ Загружаем данные...")
    key = f"{year}-{month:02d}"
    if key not in load_open_months():
        await message.edit_text("⛔ *Этот месяц закрыт для записи*", parse_mode="Markdown")
        return

    days_status = await cal.update_calendar_status(year, month)
    reply_markup = generate_calendar(year, month, days_status)

    combined_keyboard = get_main_menu(message.chat_id).inline_keyboard + reply_markup.inline_keyboard
    full_reply_markup = InlineKeyboardMarkup(combined_keyboard)

    await message.edit_text("📅 Выберите дату:", reply_markup=full_reply_markup)
    await loading_message.delete()
    

async def change_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Change month callback received.")
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    try:
        parts = query.data.split("_")
        if len(parts) != 4:
            return

        direction, _, year, month = parts
        year, month = int(year), int(month)
        key = f"{year}-{month:02d}"
        if key not in load_open_months():
            await query.edit_message_text("⛔ *Этот месяц закрыт для записи*", parse_mode="Markdown")
            return

        if direction == "prev":
            month -= 1
            if month == 0:
                year -= 1
                month = 12
        elif direction == "next":
            month += 1
            if month == 13:
                year += 1
                month = 1

        cal = IrCalendar()
        days_status = {day: "❓" for day in range(1, 32)}
        reply_markup = generate_calendar(year, month, days_status)
        message = await query.edit_message_text("📅 Выберите дату:", reply_markup=reply_markup)

        asyncio.create_task(update_calendar_after_sync(message, year, month, cal, user_id))

    except Exception as e:
        logger.error("Error while processing callback data: %s", e)
        await query.edit_message_text("Произошла ошибка при обработке календаря")

async def day_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик выбора дня в календаре."""
    logger.info("Day selected callback received.")
    query = update.callback_query
    await query.answer()

    _, year, month, day = query.data.split("_")
    selected_date = datetime(int(year), int(month), int(day)).date()
    
    notice_block = ""
    notice_text = load_notice()

    if notice_text:
        notice_block = f"📣 *{notice_text}*\n\n"

    cal = IrCalendar()  # Инициализируем объект IrCalendar
    free_slots = await cal.find_free_slots_async(selected_date)

    if not free_slots:
        date_str = selected_date.strftime('%d.%m.%Y')
        text = notice_block + f"⛔ На {date_str} нет свободных окон."
        keyboard = [
            [InlineKeyboardButton("📋 Встать в лист ожидания", callback_data=f"join_wait_{year}_{month}_{day}")],
            [InlineKeyboardButton("⬅️ Назад",                   callback_data=f"calendar_back_{year}_{month}")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return
    else:
        message = notice_block + f"✅ Свободные окна на {selected_date.strftime('%d %b')}:\n"
        keyboard = []

        for slot in free_slots:
            slot_text = slot.strftime('%H:%M')
            message += f"🕒 {slot_text}\n"
            keyboard.append([InlineKeyboardButton(f"Записаться {slot_text}", callback_data=f"book_{year}_{month}_{day}_{slot_text}")])

        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"calendar_back_{year}_{month}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=message, reply_markup=reply_markup)

async def join_waitlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    # parts == ["join","wait","2025","4","24"]
    _, _, year, month, day = parts

    key = f"{year}-{int(month):02d}-{int(day):02d}"
    date_str = f"{int(day):02d}.{int(month):02d}.{year}"
    user_id = str(query.from_user.id)

    waitlist = load_waitlist()
    lst = waitlist.get(key, [])
    if user_id in lst:
        await query.edit_message_text(f"❗ Вы уже в очереди на {date_str}.")
        return

    lst.append(user_id)
    waitlist[key] = lst
    save_waitlist(waitlist)
    await query.edit_message_text(
        f"✅ Добавил вас в лист ожидания на {date_str}.\n"
        "Мы уведомим вас, как только появится окно."
    )


async def calendar_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Calendar button pressed.")
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    now = datetime.now(TZ)
    cal = IrCalendar()

    days_status = {day: "❓" for day in range(1, 32)}
    reply_markup = generate_calendar(now.year, now.month, days_status)

    combined_keyboard = get_main_menu(user_id).inline_keyboard + reply_markup.inline_keyboard
    full_reply_markup = InlineKeyboardMarkup(combined_keyboard)

    try:
        if query.message.text:
            await query.edit_message_text("📅 Выберите дату:", reply_markup=full_reply_markup)
        else:
            await query.message.delete()
            await query.message.reply_text("📅 Выберите дату:", reply_markup=full_reply_markup)
    except Exception as e:
        logger.error(f"Ошибка при обработке calendar_open: {e}")
        await query.message.delete()
        await query.message.reply_text("📅 Выберите дату:", reply_markup=full_reply_markup)

    key = f"{now.year}-{now.month:02d}"
    if key in load_open_months():
        asyncio.create_task(update_calendar_after_sync(query.message, now.year, now.month, cal, user_id))
    else:
        await query.message.edit_text("⛔ *Этот месяц закрыт для записи*", parse_mode="Markdown")

async def calendar_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Back to calendar callback received.")
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    now = datetime.now(TZ)
    cal = IrCalendar()

    days_status = {day: "❓" for day in range(1, 32)}
    reply_markup = generate_calendar(now.year, now.month, days_status)
    message = await query.edit_message_text("📅 Выберите дату:", reply_markup=reply_markup)

    key = f"{now.year}-{now.month:02d}"
    if key in load_open_months():
        asyncio.create_task(update_calendar_after_sync(message, now.year, now.month, cal, user_id))
    else:
        await message.edit_text("⛔ *Этот месяц закрыт для записи*", parse_mode="Markdown")


async def price_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки Прайс."""
    logger.info("Price button pressed.")
    query = update.callback_query
    await query.answer()

    # Удаляем предыдущее сообщение, чтобы избежать ошибки при возврате к календарю
    try:
        await query.message.delete()
    except Exception as e:
        logger.error(f"Ошибка удаления сообщения: {e}")

    # Отправляем новое сообщение с фото
    with open('price.jpg', 'rb') as photo:
        await query.message.reply_photo(photo, caption="💵 Вот наш прайс:", reply_markup=get_main_menu())


async def contacts_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки Контакты."""
    logger.info("Contacts button pressed.")
    query = update.callback_query
    await query.answer()

    phone_number = PHONE
    await query.message.reply_text(f"📞 Наш номер телефона: {phone_number}", reply_markup=get_main_menu(int(query.from_user.id)))


def load_users():
    """Загружает список подписчиков из файла."""
    if not os.path.exists(USERS_FILE):
        return []
    with open(USERS_FILE, "r") as file:
        return json.load(file)

def save_users(users):
    """Сохраняет список подписчиков в файл."""
    with open(USERS_FILE, "w") as file:
        json.dump(users, file, indent=4)

# Загружаем подписчиков при запуске
subscribers = load_users()

async def book_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запись на выбранный слот."""
    logger.info("Booking slot...")
    query = update.callback_query
    await query.answer()

    _, year, month, day, time = query.data.split("_")
    selected_date = f"{day}.{month}.{year} в {time}"

    admin_id = "5328759519"
    message_admin = f"🔔 Новый запрос на запись: {selected_date}"
    
    # Отправляем сообщение админу
    await context.bot.send_message(chat_id=admin_id, text=message_admin)

    # Подтверждение пользователю
    await query.edit_message_text(f"✅ Запрос на запись отправлен {selected_date}!")


async def book_appointment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, year, month, day, slot = query.data.split("_")
    selected_date = datetime(int(year), int(month), int(day)).strftime('%d.%m.%Y')

    user = query.from_user
    user_id = user.id
    user_name = user.full_name

    booking_id = str(uuid.uuid4())
    booking_data = {
        "id": booking_id,
        "user_id": user_id,
        "name": user_name,
        "date": selected_date,
        "slot": slot,
        "status": "pending"
    }

    # Загрузка профилей
    profiles = load_profiles()
    user_key = str(user_id)

    # Если у пользователя нет профиля — отправляем анкету
    if user_key not in profiles:
        logger.info(f"[Booking] Новый пользователь {user_id}, начинаем анкету перед записью")
        context.user_data["confirm_booking_id"] = booking_id
        pending_bookings[booking_id] = {
            "user_id": user_id,
            "name": user_name,
            "date": selected_date,
            "slot": slot
        }

        # 💾 Сохраняем в файл
        bookings = load_bookings()
        bookings.append(booking_data)
        save_bookings(bookings)

        # Запрос анкеты
        await query.edit_message_text("📋 Для записи, пожалуйста, заполните ваш профиль.\n\nВведите ваше *имя*:")

        # ⏰ Запускаем напоминание через 5 минут
        async def remind_if_no_profile():
            await asyncio.sleep(300)  # 5 минут
            profiles_check = load_profiles()
            if user_key not in profiles_check and context.user_data.get("confirm_booking_id") == booking_id:
                logger.info(f"[Booking] Напоминание пользователю {user_id} о незавершённой анкете")
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"⏰ Вы начали запись на {selected_date} в {slot}, но не завершили анкету.\nХотите продолжить?",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📋 Продолжить", callback_data="calendar_open")]
                    ])
                )

        asyncio.create_task(remind_if_no_profile())
        return ASK_FIRST_NAME

    # ✅ Если профиль уже есть — сразу подтверждение админу
    logger.info(f"[Booking] Пользователь {user_id} записывается без анкеты — профиль уже есть")

    pending_bookings[booking_id] = {
        "user_id": user_id,
        "name": user_name,
        "date": selected_date,
        "slot": slot
    }

    # 💾 Сохраняем
    bookings = load_bookings()
    bookings.append(booking_data)
    save_bookings(bookings)

    # 🔔 Админу
    buttons = [
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_{booking_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{booking_id}")
        ]
    ]
    text = (
        f"📬 *Новая заявка*\n"
        f"👤 [{user_name}](tg://user?id={user_id})\n"
        f"📅 *Дата:* {selected_date}\n"
        f"🕒 *Время:* {slot}"
    )
    await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    # 🔄 Пользователю
    await query.edit_message_text(f"🕒 Запрос на запись отправлен!\n\nОжидайте подтверждения администратора.")


async def handle_admin_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, booking_id = query.data.split("_", 1)
    booking = pending_bookings.get(booking_id)

    if not booking:
        await query.edit_message_text("❌ Заявка уже обработана или не найдена.")
        return

    user_id = booking["user_id"]
    user_name = booking["name"]
    slot_info = f"{booking['date']} в {booking['slot']}"
    # Обновляем статус заявки
    bookings = load_bookings()
    for b in bookings:
        if b["id"] == booking_id:
            b["status"] = "confirmed" if action == "confirm" else "rejected"
            break
    save_bookings(bookings)

    # Удаляем из памяти
    del pending_bookings[booking_id]

    if action == "reject":
        await context.bot.send_message(user_id, f"❌ К сожалению, ваша запись на *{slot_info}* была отклонена.", parse_mode="Markdown")
        await query.edit_message_text(f"❌ Заявка на {slot_info} отклонена.")
        return

    # 📌 Всегда уведомляем о записи
    await context.bot.send_message(user_id, f"✅ Ваша запись на *{slot_info}* подтверждена!", parse_mode="Markdown")
    await query.edit_message_text(f"✅ Заявка на {slot_info} подтверждена.")

    # --- Новый блок: создаём событие в календаре ---
    # Парсим дату и время из booking
    year, month, day = map(int, booking["date"].split(".")[::-1])  # 'dd.mm.YYYY'
    hour, minute = map(int, booking["slot"].split(":"))
    # Формируем summary из профиля
    profiles = load_profiles()
    prof = profiles.get(str(user_id), {})
    phone = prof.get("phone", "")
    name  = prof.get("first_name", "")
    last  = prof.get("last_name", "")
    summary = f"{phone} {name} {last}"

    created = create_event(year, month, day, hour, minute, 1, summary)
    if not created:
        # Если слот вдруг оказался занят, сообщаем админу
        await context.bot.send_message(
            ADMIN_ID,
            f"❗ Не удалось добавить событие в календарь: слот {slot_info} уже занят."
        )
    # --- Конец нового блока ---

    # 📋 Проверяем профиль
    profiles = load_profiles()
    user_key = str(user_id)
    if user_key not in profiles:
        context.user_data["confirm_booking_id"] = booking_id  # 👈 ВОТ ЗДЕСЬ
        await context.bot.send_message(
            user_id,
            "📋 Чтобы в будущем записываться быстрее, пожалуйста, заполните ваш профиль.\n\nВведите ваше *имя*:"
        )
        return ASK_FIRST_NAME  # 👈 запустить анкету
    else:
        # Добавить запись в историю (если профиль есть)
        history = profiles[user_key].get("history", [])
        history.append(slot_info)
        profiles[user_key]["history"] = history
        save_profiles(profiles)

async def show_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет прав.")
        return

    bookings = load_bookings()
    if not bookings:
        await update.message.reply_text("📭 Заявок пока нет.")
        return

    # Можно добавить фильтр по статусу: ?status=confirmed/pending/etc
    lines = ["📋 *Список заявок:*", ""]
    for b in bookings[-20:][::-1]:  # последние 20, сверху — новые
        lines.append(
            f"👤 *{b['name']}*\n"
            f"📅 {b['date']} — 🕒 {b['slot']}\n"
            f"📌 Статус: `{b['status']}`\n"
            f"{'─' * 25}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")



async def send_price_html(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет ссылку на HTML-прайс во встроенном браузере Telegram."""
    price_url = PRICE_URL  # Укажите ваш URL

    message_text = f"💰 *Прайс-лист*\n\n🔗 [Открыть прайс в браузере]({price_url})\n\n"

    keyboard = [
        [InlineKeyboardButton("🔗 Открыть прайс", url=price_url)],
        [InlineKeyboardButton("📅 Текущий месяц", callback_data="calendar_open"),
         InlineKeyboardButton("📞 Контакты", callback_data="contacts_button")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(message_text, parse_mode="Markdown", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(message_text, parse_mode="Markdown", reply_markup=reply_markup)


async def send_price_html2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет HTML-файл прайса пользователю."""
    logger.info("Sending price.html to user.")

    # **1. Создаем HTML-файл, если его нет**
    file_path = "price.html"
    if not os.path.exists(file_path):
        html_content = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Прайс-лист</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Dancing+Script:wght@500&display=swap');
        body { font-family: 'Dancing Script', cursive; text-align: center; background-color: #f9f9f9; font-size: 18px; }
        h1 { color: #333; font-size: 28px; }
        h2 { color: #444; margin-top: 30px; font-size: 24px; }
        table { width: 80%; margin: 20px auto; border-collapse: collapse; background: white;
            box-shadow: 0 0 10px rgba(0, 0, 0, 0.1); border-radius: 8px; overflow: hidden; }
        th, td { border: 1px solid #ddd; padding: 12px; text-align: left; }
        th { background-color: #e91e63; color: white; font-size: 16px; }
        td { font-size: 18px; }
        .footer { margin-top: 30px; font-size: 20px; }
        .footer a { text-decoration: none; color: #e91e63; font-weight: bold; }
    </style>
</head>
<body>
    <h1>Прайс-лист</h1>
    <p>Актуально с 01.07.2024</p>
    <h2>Наращивание ресниц</h2>
    <table><tr><th>Услуга</th><th>Цена (₽)</th></tr>
        <tr><td>Уголки</td><td>2200</td></tr><tr><td>1D</td><td>2500</td></tr>
        <tr><td>1.5D</td><td>2700</td></tr><tr><td>2D</td><td>2800</td></tr>
        <tr><td>2.5D</td><td>3000</td></tr><tr><td>3D</td><td>3100</td></tr>
        <tr><td>4D</td><td>3400</td></tr><tr><td>Цветные ресницы, блестки, лучики</td><td>+300</td></tr>
    </table>
    <h2>Снятие</h2>
    <table><tr><th>Услуга</th><th>Цена (₽)</th></tr>
        <tr><td>Моей работы с последующим наращиванием</td><td>Бесплатно</td></tr>
        <tr><td>Моей работы без последующего наращивания</td><td>300</td></tr>
        <tr><td>Работы другого мастера</td><td>300</td></tr>
    </table>
    <div class="footer"><a href="https://t.me/LashesButovo_bot">🔙 Вернуться в бота</a></div>
</body>
</html>"""
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(html_content)

    # **2. Проверяем, откуда пришел запрос**
    if update.message:
        # Запрос через команду /price_html
        chat_id = update.message.chat_id
        send_method = update.message.reply_document
    elif update.callback_query:
        # Запрос через кнопку
        chat_id = update.callback_query.message.chat_id
        send_method = update.callback_query.message.reply_document
        await update.callback_query.answer()

    # **3. Отправляем файл пользователю**
    with open(file_path, "rb") as file:
        await send_method(file, caption="📄 Откройте файл, чтобы посмотреть прайс-лист.", reply_markup=get_main_menu())

EDIT_PRICE, EDIT_ITEM, EDIT_FIELD = range(3)
price_items = []  # Глобальный список для хранения услуг

def parse_html_price():
    """Парсит HTML и возвращает список [(раздел, услуга, цена)]"""
    from bs4 import BeautifulSoup
    with open("price.html", "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    result = []
    current_section = None
    for tag in soup.find_all(["h2", "tr"]):
        if tag.name == "h2":
            current_section = tag.text.strip()
        elif tag.name == "tr":
            cols = tag.find_all("td")
            if len(cols) == 2:
                result.append((current_section, cols[0].text.strip(), cols[1].text.strip()))
    return result


async def edit_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Доступ запрещён.")
        return ConversationHandler.END

    global price_items
    price_items = parse_html_price()

    keyboard = [
        [InlineKeyboardButton(f"{name} — {price}", callback_data=f"edit_{i}")]
        for i, (_, name, price) in enumerate(price_items)
    ]
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit")])
    keyboard.append([InlineKeyboardButton("📅 Текущий месяц", callback_data="calendar_open")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text("🛠 Выберите услугу для редактирования:", reply_markup=reply_markup)
    return EDIT_ITEM

async def edit_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_edit":
        await query.edit_message_text("❌ Редактирование отменено.")
        return ConversationHandler.END

    index = int(query.data.split("_")[1])
    context.user_data["edit_index"] = index
    section, name, price = price_items[index]

    keyboard = [
        [InlineKeyboardButton("✏️ Изменить название", callback_data="edit_name")],
        [InlineKeyboardButton("💰 Изменить цену", callback_data="edit_price")],
        [InlineKeyboardButton("💾 Сохранить", callback_data="save_edit")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit")]
    ]
    text = f"🔧 Вы выбрали: *{name}* — *{price}* (в разделе _{section}_)"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return EDIT_FIELD

async def edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == "edit_name":
        context.user_data["edit_field"] = "name"
        await query.edit_message_text("✏️ Введите новое название:")
        return EDIT_FIELD

    elif action == "edit_price":
        context.user_data["edit_field"] = "price"
        await query.edit_message_text("💰 Введите новую цену:")
        return EDIT_FIELD

    elif action == "save_edit":
        # Сохраняем HTML
        update_price_html()
        upload_price_to_github()  # 👈 ДОБАВЬ ЭТО
        # Заново загружаем price_items
        global price_items
        price_items = parse_html_price()

        # Кнопки
        keyboard = [
            [InlineKeyboardButton(f"{name} — {price}", callback_data=f"edit_{i}")]
            for i, (_, name, price) in enumerate(price_items)
        ]
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit")])

        await query.edit_message_text(
            "✅ Изменения сохранены. Выберите услугу для редактирования:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return EDIT_ITEM

    elif action == "cancel_edit":
        await query.edit_message_text("❌ Редактирование отменено.")
        return ConversationHandler.END



async def receive_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    index = context.user_data.get("edit_index")
    field = context.user_data.get("edit_field")
    new_value = update.message.text.strip()

    if field == "name":
        price_items[index] = (price_items[index][0], new_value, price_items[index][2])
    elif field == "price":
        price_items[index] = (price_items[index][0], price_items[index][1], new_value)

    # Показываем обновлённые данные и кнопки
    section, name, price = price_items[index]
    text = f"🔧 Вы редактируете: *{name}* — *{price}* (в разделе _{section}_)"

    keyboard = [
        [InlineKeyboardButton("✏️ Изменить название", callback_data="edit_name")],
        [InlineKeyboardButton("💰 Изменить цену", callback_data="edit_price")],
        [InlineKeyboardButton("💾 Сохранить", callback_data="save_edit")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit")]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return EDIT_FIELD


def update_price_html():
    from bs4 import BeautifulSoup
    with open("price.html", "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    tables = soup.find_all("table")
    index = 0

    for table in tables:
        for tr in table.find_all("tr")[1:]:  # Пропускаем заголовок
            section, name, price = price_items[index]
            tds = tr.find_all("td")
            if len(tds) == 2:
                tds[0].string = name
                tds[1].string = price
            index += 1

    # Перезаписываем тот же самый файл
    import shutil
    shutil.copy("price.html", f"price_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")

    with open("price.html", "w", encoding="utf-8") as f:
        f.write(str(soup))
    #new_filename = f"price_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.html"
    #with open(new_filename, "w", encoding="utf-8") as f:
    #    f.write(str(soup))


def main():
    """Запуск бота."""
    logger.info("Bot started.")
    application = Application.builder().token(TOKEN).build()

    booking_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(book_appointment, pattern=r"^book_\d+_\d+_\d+_\d+:\d+$")
        ],
        states={
            ASK_FIRST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_first_name)],
            ASK_LAST_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_last_name)],
            ASK_PHONE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
        },
        fallbacks=[],
        per_user=True,
    )
    application.add_handler(booking_conv)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(change_month, pattern=r"^(prev|next)_month_"))
    application.add_handler(CallbackQueryHandler(day_selected, pattern=r"^day_\d+_\d+_\d+"))
    application.add_handler(CallbackQueryHandler(calendar_open, pattern="calendar_open"))
    application.add_handler(CallbackQueryHandler(calendar_back, pattern=r"^calendar_back_\d+_\d+$"))

    setup_secret_easteregg(application)

    # Обработчики для новых кнопок
    application.add_handler(CallbackQueryHandler(price_button, pattern="price_button"))
    application.add_handler(CallbackQueryHandler(contacts_button, pattern="contacts_button"))
    application.add_handler(CommandHandler("subscribers", subscribers_count))
    application.add_handler(CommandHandler("price_html", send_price_html))
    application.add_handler(CallbackQueryHandler(send_price_html, pattern="price_html"))
    application.add_handler(get_faq_handler())
    application.add_handler(get_faq_callback_handler())
    application.add_handler(get_faq_menu_handler())
    application.add_handler(CommandHandler("open_month", open_month_command))
    application.add_handler(CallbackQueryHandler(admin_open_month_button, pattern=r"^admin_open_\d{4}-\d{2}$"))
    application.add_handler(CommandHandler("bookings", show_bookings))
    application.add_handler(CallbackQueryHandler(show_user_bookings, pattern="user_bookings"))
    application.add_handler(CallbackQueryHandler(user_cancel_booking, pattern=r"^user_cancel_[\w-]+$"))
    application.add_handler(CallbackQueryHandler(show_user_profile, pattern="user_profile"))
    application.add_handler(CallbackQueryHandler(admin_month_bookings, pattern="admin_month_bookings"))
    application.add_handler(CallbackQueryHandler(admin_free_slots_month, pattern="admin_free_slots_month"))
    application.add_handler(CallbackQueryHandler(handle_admin_buttons, pattern=r"^admin_(open_month|subscribers)$"))

    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(handle_admin_response, pattern=r"^(confirm|reject)_[\w-]+$")
        ],
        states={
            ASK_FIRST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_first_name)],
            ASK_LAST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_last_name)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
        },
        fallbacks=[]
    )
    application.add_handler(admin_conv)

    admin_profile_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_edit_user_profile, pattern="^admin_edit_profile$")
        ],
        states={
            EDIT_USER_ID: [CallbackQueryHandler(admin_select_user_field, pattern="^edit_user_\\d+$|^cancel_edit_user$")],
            EDIT_FIELD_CHOICE: [CallbackQueryHandler(admin_input_user_field, pattern="^edit_field_\\w+$")],
            EDIT_FIELD_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_save_user_field)]
        },
        fallbacks=[]
    )
    application.add_handler(admin_profile_conv)

    edit_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("edit_price", edit_price)],
            states={
                EDIT_ITEM: [CallbackQueryHandler(edit_item, pattern=r"^edit_\d+$|^cancel_edit$")],
                EDIT_FIELD: [
                    CallbackQueryHandler(edit_field, pattern=r"^edit_name$|^edit_price$|^save_edit$|^cancel_edit$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_input)
                ]
            },
            fallbacks=[]
        )

    notice_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_notice_start, pattern="^admin_notice$")],
        states={NOTICE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_notice_save)]},
        fallbacks=[],
        #per_message=True,         # чтобы ловить каждое новое сообщение
        allow_reentry=True
    )
    application.add_handler(notice_conv, group=-1)

    application.add_handler(edit_conv_handler)
    application.add_handler(
        CallbackQueryHandler(join_waitlist, pattern=r"^join_wait_\d+_\d+_\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(view_waitlist, pattern="^view_waitlist$")
    )
    application.add_handler(
        CallbackQueryHandler(cancel_waitlist, pattern=r"^cancel_wait_\d{4}-\d{2}-\d{2}$")
    )

    application.run_polling()


if __name__ == "__main__":
    main()

