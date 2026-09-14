import logging
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ID чата/канала, куда будут падать регистрации и вопросы (например, ваш личный chat_id или id группы)
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))


def start(update: Update, context: CallbackContext) -> None:
    keyboard = [
        [InlineKeyboardButton("Регистрация на вебинар", callback_data='register')],
        [InlineKeyboardButton("Вопросы для спикера", callback_data='questions')],
        [InlineKeyboardButton("Помощь", callback_data='help')],
        [InlineKeyboardButton("Все курсы и вебинары Tillo", callback_data='courses')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text('Выберите пункт меню:', reply_markup=reply_markup)


def button(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()

    if query.data == 'register':
        query.edit_message_text(text="Введите ваш ник и имя:")
        context.user_data['state'] = 'REGISTER'
    elif query.data == 'questions':
        query.edit_message_text(text="Введите ваш вопрос для спикера:")
        context.user_data['state'] = 'QUESTIONS'
    elif query.data == 'help':
        query.edit_message_text(
            text="Если что-то непонятно или появились вопросы про вебинар — "
                 "пишите мне в личные сообщения @AppolaAppola"
        )
    elif query.data == 'courses':
        query.edit_message_text(
            text="Список курсов и вебинаров Tillo:\n1. Вебинар 1\n2. Вебинар 2\n3. Вебинар 3"
        )


def handle_message(update: Update, context: CallbackContext) -> None:
    state = context.user_data.get('state')
    user = update.effective_user

    if state == 'REGISTER':
        update.message.reply_text(f"Вы зарегистрированы с ником: {update.message.text}")
        if ADMIN_CHAT_ID:
            context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"Новая регистрация от @{user.username} ({user.id}): {update.message.text}",
            )
        context.user_data['state'] = None

    elif state == 'QUESTIONS':
        update.message.reply_text(f"Ваш вопрос: {update.message.text} отправлен спикеру.")
        if ADMIN_CHAT_ID:
            context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"Вопрос от @{user.username} ({user.id}): {update.message.text}",
            )
        context.user_data['state'] = None

    else:
        update.message.reply_text("Нажмите /start, чтобы открыть меню.")


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

    updater = Updater(token, use_context=True)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler('start', start))
    dispatcher.add_handler(CallbackQueryHandler(button))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    # Render автоматически задаёт RENDER_EXTERNAL_URL — если она есть, работаем через вебхук.
    # Если её нет (например, запуск на своём компьютере) — обычный polling.
    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    port = int(os.environ.get("PORT", "10000"))

    if external_url:
        updater.start_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=token,
            webhook_url=f"{external_url}/{token}",
        )
        logger.info("Бот запущен через вебхук: %s", external_url)
    else:
        updater.start_polling()
        logger.info("Бот запущен через polling")

    updater.idle()


if __name__ == '__main__':
    main()
