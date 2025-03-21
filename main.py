import logging
import asyncio
import caldav
from icalendar import Calendar
from datetime import datetime, time, timedelta
import pytz
import locale
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import json
import os

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

    async def update_calendar_status(self, year, month):
        """Обновляет календарь, помечая дни как ✅ или ⛔."""
        logger.info("Updating calendar status for year: %d, month: %d", year, month)
        days_status = {}

        # Для каждого дня в месяце проверяем наличие свободных слотов
        for day in range(1, 32):
            try:
                selected_date = datetime(year, month, day).date()
                free_slots = await self.find_free_slots_async(selected_date)

                if free_slots:
                    days_status[day] = "✅"  # Если есть хотя бы один свободный слот
                else:
                    days_status[day] = "⛔"  # Если все слоты заняты

                logger.debug("Day %d status: %s", day, days_status[day])
            except ValueError:
                # Если дата некорректна (например, 31 сентября), пропускаем
                logger.warning("Invalid date: %d/%d/%d", day, month, year)
                continue

        logger.info("Updated calendar status: %s", days_status)
        return days_status


def generate_calendar(year, month, days_status, mode="auto"):
    """
    Создает inline-календарь на указанный месяц, адаптируя под телефон или компьютер.
    mode = "phone" → 7 кнопок в строке (неделя).
    mode = "desktop" → 10 кнопок в строке.
    mode = "auto" → автоопределение.
    """
    logger.debug("Generating calendar for year: %d, month: %d, mode: %s", year, month, mode)

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

    # Заполняем календарь днями с новыми символами
    for day in range(1, last_day.day + 1):
        status = days_status.get(day, "❓")

        # Изменяем отображение дней
        if status == "✅":
            day_text = f"{day}"  # Свободный день
        elif status == "⛔":
            day_text = f"❌"  # Занятый день
        else:
            day_text = f"{day}"  # Неизвестный статус

        row.append(InlineKeyboardButton(day_text, callback_data=f"day_{year}_{month}_{day}"))

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


def get_main_menu():
    """Создает меню с основными кнопками."""
    keyboard = [
        [InlineKeyboardButton("📅 Календарь", callback_data="calendar_open")],
        #[InlineKeyboardButton("💵 Прайс", callback_data="price_button")],
        [InlineKeyboardButton("📄 Прайс", callback_data="price_html")],  # Новая кнопка
        [InlineKeyboardButton("📞 Контакты", callback_data="contacts_button")]
    ]
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

    combined_keyboard = get_main_menu().inline_keyboard + reply_markup.inline_keyboard
    full_reply_markup = InlineKeyboardMarkup(combined_keyboard)

    message = await update.message.reply_text("📅 Выберите дату:", reply_markup=full_reply_markup)

    asyncio.create_task(update_calendar_after_sync(message, now.year, now.month, cal))


async def subscribers_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /subscribers: Показывает список подписчиков (только для администраторов)."""
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет прав для выполнения этой команды.")
        return

    count = len(subscribers)
    message = f"📊 *Всего подписчиков: {count}*\n\n"

    if count == 0:
        message += "❌ Нет подписчиков."
    else:
        for sub in subscribers:
            username_display = f"🔗 @{sub['username']}" if sub['username'] else "🔗 Без юзернейма"
            message += (
                f"👤 *{sub['name']}* (ID: `{sub['id']}`)\n"
                f"📅 Подписался: {sub['date_subscribed']}\n"
                f"{username_display}\n"
                f"-----------------------------\n"
            )

    # Кнопки для навигации
    keyboard = [
        [InlineKeyboardButton("📅 Календарь", callback_data="calendar_open")],
        [InlineKeyboardButton("💵 Прайс", callback_data="price_button")],
        [InlineKeyboardButton("📞 Контакты", callback_data="contacts_button")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(message, parse_mode="Markdown", reply_markup=reply_markup)

async def update_calendar_after_sync(message, year, month, cal):
    """Фоновая задача обновления календаря после синхронизации с Yandex Календарем."""
    
    # Отправляем пользователю сообщение о загрузке
    loading_message = await message.reply_text("⏳ Загружаем данные...")

    # Получаем данные
    days_status = await cal.update_calendar_status(year, month)  # Получаем статусы занятости
    reply_markup = generate_calendar(year, month, days_status)  # Создаем обновленный календарь

    # Объединяем календарь с основным меню
    combined_keyboard = get_main_menu().inline_keyboard + reply_markup.inline_keyboard
    full_reply_markup = InlineKeyboardMarkup(combined_keyboard)

    # Обновляем сообщение календаря
    await message.edit_text("📅 Выберите дату:", reply_markup=full_reply_markup)

    # Удаляем сообщение о загрузке после завершения
    await loading_message.delete()
    
async def update_calendar_after_sync2(message, year, month, cal):
    """Фоновая задача обновления календаря после синхронизации с Яндекс Календарем."""
    days_status = await cal.update_calendar_status(year, month)  # Получаем статусы занятости
    reply_markup = generate_calendar(year, month, days_status)  # Создаем обновленный календарь

    # Объединяем календарь с основным меню
    combined_keyboard = get_main_menu().inline_keyboard + reply_markup.inline_keyboard
    full_reply_markup = InlineKeyboardMarkup(combined_keyboard)

    # Обновляем сообщение с календарем, сохраняя меню
    await message.edit_text("📅 Выберите дату:", reply_markup=full_reply_markup)


async def change_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик для смены месяца в календаре."""
    logger.info("Change month callback received.")
    query = update.callback_query
    await query.answer()

    try:
        parts = query.data.split("_")
        if len(parts) != 4:
            return

        direction, _, year, month = parts
        year, month = int(year), int(month)

        # Логика изменения месяца
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

        cal = IrCalendar()  # Инициализируем объект IrCalendar

        # 1. Показываем календарь сразу с ❓
        days_status = {day: "❓" for day in range(1, 32)}
        reply_markup = generate_calendar(year, month, days_status)
        message = await query.edit_message_text("📅 Выберите дату:", reply_markup=reply_markup)

        # 2. Загружаем статусы с Яндекс Календаря **асинхронно**
        asyncio.create_task(update_calendar_after_sync(message, year, month, cal))

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

    cal = IrCalendar()  # Инициализируем объект IrCalendar
    free_slots = await cal.find_free_slots_async(selected_date)

    if not free_slots:
        message = f"⛔ На {selected_date.strftime('%d %b')} нет свободных окон."
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data=f"calendar_back_{year}_{month}")]]
    else:
        message = f"✅ Свободные окна на {selected_date.strftime('%d %b')}:\n"
        keyboard = []

        for slot in free_slots:
            slot_text = slot.strftime('%H:%M')
            message += f"🕒 {slot_text}\n"
            keyboard.append([InlineKeyboardButton(f"Записаться {slot_text}", callback_data=f"book_{year}_{month}_{day}_{slot_text}")])

        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"calendar_back_{year}_{month}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=message, reply_markup=reply_markup)



async def calendar_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открытие календаря при нажатии на кнопку."""
    logger.info("Calendar button pressed.")
    query = update.callback_query
    await query.answer()

    now = datetime.now(TZ)
    cal = IrCalendar()

    # Сначала показываем календарь с неизвестными статусами
    days_status = {day: "❓" for day in range(1, 32)}
    reply_markup = generate_calendar(now.year, now.month, days_status)

    # Объединяем календарь с основным меню
    combined_keyboard = get_main_menu().inline_keyboard + reply_markup.inline_keyboard
    full_reply_markup = InlineKeyboardMarkup(combined_keyboard)

    # Проверяем, можно ли редактировать сообщение
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

    # Асинхронно обновляем календарь
    asyncio.create_task(update_calendar_after_sync(query.message, now.year, now.month, cal))


async def calendar_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возвращение к календарю (назад)."""
    logger.info("Back to calendar callback received.")
    query = update.callback_query
    await query.answer()

    now = datetime.now(TZ)
    cal = IrCalendar()

    # 1. Показываем календарь сразу с ❓
    days_status = {day: "❓" for day in range(1, 32)}
    reply_markup = generate_calendar(now.year, now.month, days_status)
    message = await query.edit_message_text("📅 Выберите дату:", reply_markup=reply_markup)

    # 2. Асинхронно обновляем статусы с Яндекс Календаря
    asyncio.create_task(update_calendar_after_sync(message, now.year, now.month, cal))


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
    await query.message.reply_text(f"📞 Наш номер телефона: {phone_number}", reply_markup=get_main_menu())

# Список ID администраторов
#ADMIN_IDS = [5328759519,173968578]  # Добавьте ID всех администраторов

#ADMIN_ID = 5328759519  # ID администратора

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
    """Обработчик кнопки 'Записаться' - отправляет сообщение администратору."""
    logger.info("Booking appointment request received.")
    query = update.callback_query
    await query.answer()

    _, year, month, day, slot = query.data.split("_")
    selected_date = datetime(int(year), int(month), int(day)).strftime('%d %b %Y')

    user = query.from_user
    user_name = user.full_name
    user_id = user.id

    booking_message = (
        f"📅 *Новая запись!*\n"
        f"👤 *Пользователь:* [{user_name}](tg://user?id={user_id})\n"
        f"📅 *Дата:* {selected_date}\n"
        f"🕒 *Время:* {slot}\n"
    )

    # Отправка администратору
    await context.bot.send_message(chat_id=ADMIN_ID, text=booking_message, parse_mode="Markdown")

    # Подтверждение пользователю
    await query.edit_message_text(f"✅ Запрос на запись на *{selected_date} в {slot}* отправлен!\n\nАдминистратор скоро свяжется с вами.", parse_mode="Markdown")

async def send_price_html(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет ссылку на HTML-прайс во встроенном браузере Telegram."""
    price_url = PRICE_URL  # Укажите ваш URL

    message_text = f"💰 *Прайс-лист*\n\n🔗 [Открыть прайс в браузере]({price_url})\n\n"

    keyboard = [
        [InlineKeyboardButton("🔗 Открыть прайс", url=price_url)],
        [InlineKeyboardButton("📅 Календарь", callback_data="calendar_open"),
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

def main():
    """Запуск бота."""
    logger.info("Bot started.")
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(change_month, pattern=r"^(prev|next)_month_"))
    application.add_handler(CallbackQueryHandler(day_selected, pattern=r"^day_\d+_\d+_\d+"))
    application.add_handler(CallbackQueryHandler(calendar_open, pattern="calendar_open"))
    #application.add_handler(CallbackQueryHandler(calendar_back, pattern="calendar_back"))
    application.add_handler(CallbackQueryHandler(calendar_back, pattern=r"^calendar_back_\d+_\d+$"))

    
    # Обработчики для новых кнопок
    application.add_handler(CallbackQueryHandler(price_button, pattern="price_button"))
    application.add_handler(CallbackQueryHandler(contacts_button, pattern="contacts_button"))
    application.add_handler(CallbackQueryHandler(book_appointment, pattern=r"^book_\d+_\d+_\d+_\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(book_slot, pattern=r"^book_\d+_\d+_\d+_\d+:\d+$"))
    application.add_handler(CommandHandler("subscribers", subscribers_count))
    application.add_handler(CommandHandler("price_html", send_price_html))
    application.add_handler(CallbackQueryHandler(send_price_html, pattern="price_html"))


    application.run_polling()


if __name__ == "__main__":
    main()