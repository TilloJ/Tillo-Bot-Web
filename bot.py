import datetime
import logging
import os
import time

import pytz
import tornado.web
from telegram import Update, BotCommand
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)
from telegram.ext.utils import webhookhandler as _webhookhandler
from telegram.error import TelegramError

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ID группы, куда падают регистрации и вопросы. Задаётся в настройках Render.
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))

# ============================================================================
# РАСПИСАНИЕ ВЕБИНАРОВ
# Добавляйте и меняйте строки здесь. Дата строго в формате ДД.ММ.ГГГГ.
# За день до каждой даты бот сам разошлёт напоминание всем, кто записался.
# ============================================================================

WEBINARS = [
    {
        "date": "30.09.2026",
        "time": "19:00",
        "title": "Черепанова Екатерина: 5 ошибок, из-за которых здоровое "
                 "питание не становится образом жизни",
    },
    {
        "date": "19.10.2026",
        "time": "19:00",
        "title": "Галеева Ирина: Упадок сил, выгорание или гормональный сбой? "
                 "Как понять, что происходит с организмом",
    },
    {
        "date": "30.10.2026",
        "time": "19:00",
        "title": "Крумкач Ольга: Гормоны и энергия или женское здоровье "
                 "без мифов",
    },
]

# Часовой пояс и время, в которое уходит напоминание накануне вебинара
TIMEZONE = pytz.timezone("Europe/Moscow")
REMINDER_HOUR = 12
REMINDER_MINUTE = 0

# ============================================================================
# ТЕКСТЫ — меняйте только то, что внутри кавычек.
# \n — перенос строки, \n\n — пустая строка между абзацами.
# Можно использовать теги: <b>жирный</b>, <i>курсив</i>, <a href="...">ссылка</a>
# ============================================================================

TEXT_START = (
    "Привет! Это бот Tillo Медиа.\n\n"
    "Нажмите кнопку <b>Меню</b> слева от поля ввода — там все действия:\n\n"
    "/register — регистрация на вебинар\n"
    "/questions — задать вопрос спикеру\n"
    "/courses — все курсы и вебинары\n"
    "/help — помощь"
)

TEXT_REGISTER = "Напишите ваше имя — я зарегистрирую вас на вебинар."

TEXT_REGISTER_DONE = (
    "Готово, вы зарегистрированы! За день до вебинара пришлю напоминание."
)

TEXT_QUESTIONS = "Напишите ваш вопрос — я передам его спикеру."

TEXT_QUESTIONS_DONE = "Спасибо! Ваш вопрос отправлен спикеру."

TEXT_HELP = (
    "Если что-то непонятно или появились вопросы про вебинар — "
    "пишите мне в личные сообщения @AppolaAppola"
)

TEXT_UNKNOWN = (
    "Нажмите кнопку <b>Меню</b> слева от поля ввода, чтобы выбрать действие."
)

# Шаблон напоминания. {title} и {time} подставляются автоматически.
TEXT_REMINDER = (
    "Привет! Напоминаем: уже <b>завтра</b> в {time} вебинар\n\n"
    "<b>{title}</b>\n\n"
    "Ждём вас 🙂"
)


def courses_text() -> str:
    """Собирает список курсов из расписания выше, чтобы не дублировать вручную."""
    lines = ["<b>Курсы и вебинары Tillo</b>", ""]
    for i, w in enumerate(WEBINARS, start=1):
        lines.append(f"{i}. <b>{w['date']}</b> в {w['time']} — {w['title']}")
        lines.append("")
    return "\n".join(lines).strip()


# ============================================================================
# ХРАНИЛИЩЕ ПОДПИСЧИКОВ
# Бесплатный Render стирает память при перезапуске, поэтому список тех, кто
# записался, хранится в закреплённом сообщении внутри рабочей группы.
# ============================================================================

REGISTRY_HEADER = "📋 СПИСОК ЗАРЕГИСТРИРОВАННЫХ (не удалять, не редактировать)"

subscribers = set()
reminded_dates = set()   # по каким вебинарам напоминание уже уходило
registry_message_id = None


def registry_text() -> str:
    ids = ",".join(str(i) for i in sorted(subscribers))
    sent = ",".join(sorted(reminded_dates))
    return (
        f"{REGISTRY_HEADER}\n"
        f"Всего: {len(subscribers)}\n"
        f"{ids}\n"
        f"SENT:{sent}"
    )


def load_registry(bot) -> None:
    """Читает список подписчиков из закреплённого сообщения в группе."""
    global registry_message_id

    if not ADMIN_CHAT_ID:
        logger.warning("ADMIN_CHAT_ID не задан — регистрации сохраняться не будут")
        return

    try:
        chat = bot.get_chat(ADMIN_CHAT_ID)
        pinned = chat.pinned_message
        if pinned and pinned.text and pinned.text.startswith(REGISTRY_HEADER):
            registry_message_id = pinned.message_id
            lines = pinned.text.split("\n")
            if len(lines) >= 3 and lines[2].strip():
                for part in lines[2].split(","):
                    part = part.strip()
                    if part.isdigit():
                        subscribers.add(int(part))
            for line in lines[3:]:
                if line.startswith("SENT:"):
                    for d in line[5:].split(","):
                        if d.strip():
                            reminded_dates.add(d.strip())
            logger.info(
                "Загружено подписчиков: %s, отправленных напоминаний: %s",
                len(subscribers), len(reminded_dates),
            )
        else:
            logger.info("Закреплённого списка нет — будет создан при первой регистрации")
    except TelegramError as e:
        logger.error("Не удалось прочитать список подписчиков: %s", e)


def save_registry(bot) -> None:
    """Обновляет закреплённое сообщение со списком подписчиков."""
    global registry_message_id

    if not ADMIN_CHAT_ID:
        return

    try:
        if registry_message_id:
            bot.edit_message_text(
                chat_id=ADMIN_CHAT_ID,
                message_id=registry_message_id,
                text=registry_text(),
            )
        else:
            msg = bot.send_message(chat_id=ADMIN_CHAT_ID, text=registry_text())
            registry_message_id = msg.message_id
            bot.pin_chat_message(
                chat_id=ADMIN_CHAT_ID,
                message_id=msg.message_id,
                disable_notification=True,
            )
    except TelegramError as e:
        logger.error("Не удалось сохранить список подписчиков: %s", e)


def is_admin_chat(update: Update) -> bool:
    """Служебные команды работают только внутри рабочей группы."""
    return bool(ADMIN_CHAT_ID) and update.effective_chat.id == ADMIN_CHAT_ID


def broadcast(bot, text: str):
    """Рассылает текст всем зарегистрированным. Возвращает (доставлено, ошибок)."""
    sent, failed = 0, []
    for user_id in list(subscribers):
        try:
            bot.send_message(chat_id=user_id, text=text, parse_mode='HTML')
            sent += 1
        except TelegramError as e:
            logger.warning("Не доставлено %s: %s", user_id, e)
            failed.append(user_id)
        time.sleep(0.05)  # чтобы не упереться в лимиты Телеграма

    for user_id in failed:
        subscribers.discard(user_id)
    if failed:
        save_registry(bot)
    return sent, len(failed)


# ============================================================================
# АВТОМАТИЧЕСКИЕ НАПОМИНАНИЯ
# ============================================================================


def send_reminders(bot, today: datetime.date = None) -> int:
    """Проверяет, есть ли вебинар завтра, и если да — рассылает напоминание."""
    if today is None:
        today = datetime.datetime.now(TIMEZONE).date()
    tomorrow = today + datetime.timedelta(days=1)

    total = 0
    for w in WEBINARS:
        try:
            webinar_date = datetime.datetime.strptime(w["date"], "%d.%m.%Y").date()
        except ValueError:
            logger.error("Неверный формат даты в расписании: %s", w["date"])
            continue

        if webinar_date != tomorrow:
            continue
        if w["date"] in reminded_dates:
            logger.info("Напоминание про %s уже отправляли", w["date"])
            continue

        text = TEXT_REMINDER.format(title=w["title"], time=w["time"])
        sent, failed = broadcast(bot, text)
        reminded_dates.add(w["date"])
        save_registry(bot)
        total += sent

        logger.info("Напоминание про %s: доставлено %s, ошибок %s",
                    w["date"], sent, failed)
        if ADMIN_CHAT_ID:
            try:
                bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=f"🔔 Отправлено напоминание про вебинар {w['date']}.\n"
                         f"Доставлено: {sent}, не доставлено: {failed}.",
                )
            except TelegramError:
                pass
    return total


def daily_job(context: CallbackContext) -> None:
    send_reminders(context.bot)


# ============================================================================
# КОМАНДЫ ДЛЯ ПОЛЬЗОВАТЕЛЕЙ
# ============================================================================


def start(update: Update, context: CallbackContext) -> None:
    context.user_data['state'] = None
    update.message.reply_text(TEXT_START, parse_mode='HTML')


def register(update: Update, context: CallbackContext) -> None:
    context.user_data['state'] = 'REGISTER'
    update.message.reply_text(TEXT_REGISTER, parse_mode='HTML')


def questions(update: Update, context: CallbackContext) -> None:
    context.user_data['state'] = 'QUESTIONS'
    update.message.reply_text(TEXT_QUESTIONS, parse_mode='HTML')


def help_command(update: Update, context: CallbackContext) -> None:
    update.message.reply_text(TEXT_HELP, parse_mode='HTML')


def courses(update: Update, context: CallbackContext) -> None:
    update.message.reply_text(courses_text(), parse_mode='HTML')


def handle_message(update: Update, context: CallbackContext) -> None:
    # В группе бот на обычные сообщения не реагирует
    if update.effective_chat.type != "private":
        return

    state = context.user_data.get('state')
    user = update.effective_user

    if state == 'REGISTER':
        update.message.reply_text(TEXT_REGISTER_DONE, parse_mode='HTML')
        subscribers.add(user.id)
        save_registry(context.bot)
        if ADMIN_CHAT_ID:
            context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"✅ Регистрация: {update.message.text}\n"
                     f"От: @{user.username or '—'} ({user.full_name})",
            )
        context.user_data['state'] = None

    elif state == 'QUESTIONS':
        update.message.reply_text(TEXT_QUESTIONS_DONE, parse_mode='HTML')
        if ADMIN_CHAT_ID:
            context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"❓ Вопрос спикеру: {update.message.text}\n"
                     f"От: @{user.username or '—'} ({user.full_name})",
            )
        context.user_data['state'] = None

    else:
        update.message.reply_text(TEXT_UNKNOWN, parse_mode='HTML')


# ============================================================================
# СЛУЖЕБНЫЕ КОМАНДЫ (работают только в рабочей группе)
# ============================================================================


def chat_id_command(update: Update, context: CallbackContext) -> None:
    """/id — показывает номер текущего чата. Нужен только при настройке."""
    update.message.reply_text(f"ID этого чата: {update.effective_chat.id}")


def stats_command(update: Update, context: CallbackContext) -> None:
    """/stats — сколько человек записано и что будет дальше."""
    if not is_admin_chat(update):
        return

    today = datetime.datetime.now(TIMEZONE).date()
    upcoming = []
    for w in WEBINARS:
        try:
            d = datetime.datetime.strptime(w["date"], "%d.%m.%Y").date()
        except ValueError:
            continue
        if d >= today:
            days = (d - today).days
            mark = " (напоминание уже отправлено)" if w["date"] in reminded_dates else ""
            upcoming.append(f"• {w['date']} — через {days} дн.{mark}")

    text = f"Зарегистрировано: {len(subscribers)} чел.\n\n"
    text += "Ближайшие вебинары:\n" + ("\n".join(upcoming) if upcoming
                                       else "— расписание пустое")
    text += f"\n\nНапоминания уходят в {REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d} " \
            f"накануне (Москва)."
    update.message.reply_text(text)


def broadcast_command(update: Update, context: CallbackContext) -> None:
    """/broadcast текст — разослать сообщение всем вручную."""
    if not is_admin_chat(update):
        return

    text = update.message.text.partition(' ')[2].strip()
    if not text:
        update.message.reply_text(
            "Напишите текст после команды, например:\n"
            "/broadcast Привет! Вебинар уже завтра в 19:00"
        )
        return

    if not subscribers:
        update.message.reply_text("Пока никто не зарегистрирован — рассылать некому.")
        return

    update.message.reply_text(f"Начинаю рассылку — получателей: {len(subscribers)}")
    sent, failed = broadcast(context.bot, text)
    update.message.reply_text(f"Готово. Доставлено: {sent}. Не доставлено: {failed}.")


def check_command(update: Update, context: CallbackContext) -> None:
    """/check — вручную запустить проверку напоминаний (для теста)."""
    if not is_admin_chat(update):
        return
    total = send_reminders(context.bot)
    if total == 0:
        update.message.reply_text(
            "Проверила: на завтра вебинаров нет (или напоминание уже отправлено)."
        )


# ============================================================================
# HEALTH-СТРАНИЦА
# Нужна, чтобы cron-job.org будил сервис и получал ответ 200, а не ошибку.
# ============================================================================


class HealthHandler(tornado.web.RequestHandler):
    SUPPORTED_METHODS = ["GET", "HEAD"]

    def get(self) -> None:
        self.set_status(200)
        self.write("OK")

    def head(self) -> None:
        self.set_status(200)


def _patched_app_init(self, webhook_path, bot, update_queue):
    self.shared_objects = {"bot": bot, "update_queue": update_queue}
    handlers = [
        (rf"{webhook_path}/?", _webhookhandler.WebhookHandler, self.shared_objects),
        (r"/.*", HealthHandler),
    ]
    tornado.web.Application.__init__(self, handlers)


_webhookhandler.WebhookAppClass.__init__ = _patched_app_init


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

    updater = Updater(token, use_context=True)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler('start', start))
    dispatcher.add_handler(CommandHandler('register', register))
    dispatcher.add_handler(CommandHandler('questions', questions))
    dispatcher.add_handler(CommandHandler('help', help_command))
    dispatcher.add_handler(CommandHandler('courses', courses))
    dispatcher.add_handler(CommandHandler('id', chat_id_command))
    dispatcher.add_handler(CommandHandler('stats', stats_command))
    dispatcher.add_handler(CommandHandler('broadcast', broadcast_command))
    dispatcher.add_handler(CommandHandler('check', check_command))
    dispatcher.add_handler(
        MessageHandler(Filters.text & ~Filters.command, handle_message)
    )

    # Именно это создаёт кнопку "Меню" рядом с полем ввода.
    # Служебные команды в меню намеренно не показываем.
    updater.bot.set_my_commands([
        BotCommand("register", "Регистрация на вебинар"),
        BotCommand("questions", "Вопросы для спикера"),
        BotCommand("courses", "Все курсы и вебинары Tillo"),
        BotCommand("help", "Помощь"),
    ])

    load_registry(updater.bot)

    # Ежедневная проверка: не начинается ли вебинар завтра
    updater.job_queue.run_daily(
        daily_job,
        time=datetime.time(hour=REMINDER_HOUR, minute=REMINDER_MINUTE,
                           tzinfo=TIMEZONE),
    )
    logger.info("Ежедневная проверка напоминаний запланирована на %02d:%02d (Москва)",
                REMINDER_HOUR, REMINDER_MINUTE)

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
