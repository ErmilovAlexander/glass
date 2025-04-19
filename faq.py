# faq.py
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
import logging

logger = logging.getLogger(__name__)

FAQ_TOPICS = [
    {
        "id": "care",
        "title": "Уход после наращивания",
        "text": (
            "\U0001FA74 *Уход после наращивания:*\n"
            "• Не мочите ресницы первые 24 часа\n"
            "• Не трите глаза и не спите лицом в подушку\n"
            "• Используйте щёточку для расчёсывания\n"
            "• Избегайте жирной косметики и масла"
        )
    },
    {
        "id": "contra",
        "title": "Противопоказания",
        "text": (
            "⚠️ *Противопоказания:*\n"
            "• Конъюнктивит, ячмень, аллергия\n"
            "• Беременность (1 триместр) — по желанию\n"
            "• Химиотерапия, глазные операции\n"
            "• Высокая чувствительность глаз"
        )
    },
    {
        "id": "before",
        "title": "Перед процедурой",
        "text": (
            "⏰ *Перед процедурой:*\n"
            "• Не наносите тушь, тени, крема и прочую косметику\n"
            "• Не употреблять кофе, зеленый чай и энергетические напитки"
        )
    },
]

def get_faq_handler():
    return CommandHandler("faq", faq)

def get_faq_callback_handler():
    return CallbackQueryHandler(handle_faq_response, pattern=r"^faq_(?!menu$)")

def get_faq_menu_handler():
    return CallbackQueryHandler(faq_menu, pattern="^faq_menu$")

async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(text=topic["title"], callback_data=f"faq_{topic['id']}")]
        for topic in FAQ_TOPICS
    ]
    keyboard.append([
        InlineKeyboardButton("📅 Текущий месяц", callback_data="calendar_open"),
        InlineKeyboardButton("📄 Прайс", callback_data="price_html"),
        InlineKeyboardButton("📞 Контакты", callback_data="contacts_button")
    ])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text("🧑‍🎓 Выберите интересующий вас вопрос:", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.edit_message_text("🧑‍🎓 Выберите интересующий вас вопрос:", reply_markup=reply_markup, parse_mode="Markdown")

async def faq_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await faq(update, context)

async def handle_faq_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    topic_id = query.data.replace("faq_", "")
    topic = next((t for t in FAQ_TOPICS if t["id"] == topic_id), None)

    if topic:
        nav_buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📅 Текущий месяц", callback_data="calendar_open"),
                InlineKeyboardButton("📄 Прайс", callback_data="price_html"),
                InlineKeyboardButton("📞 Контакты", callback_data="contacts_button")
            ],
            [InlineKeyboardButton("🧑‍🎓 Назад к вопросам", callback_data="faq_menu")]
        ])
        await query.edit_message_text(text=topic["text"], parse_mode="Markdown", reply_markup=nav_buttons)
    else:
        await query.edit_message_text("❌ Ошибка: тема не найдена.", reply_markup=None)

