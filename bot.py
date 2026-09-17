import datetime
import html
import logging
import os
import re
import time

import pytz
import tornado.web
from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)
from telegram.ext.utils import webhookhandler as _webhookhandler
from telegram.error import TelegramError, Unauthorized, BadRequest

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
# За день до каждой даты бот сам разошлёт напоминание тем, кто записался
# именно на этот вебинар.
# ============================================================================

# Каждый вебинар идёт дважды в один день: утром и вечером. Это два разных
# вебинара — у каждого свой список записавшихся, своё напоминание и своя
# ссылка на зум. Время в название писать НЕ нужно, бот показывает его сам.
WEBINARS = [
    {
        "date": "30.09.2026",
        "time": "11:00",
        "title": "Черепанова Екатерина: 5 ошибок, из-за которых здоровое "
                 "питание не становится образом жизни",
    },
    {
        "date": "30.09.2026",
        "time": "19:00",
        "title": "Черепанова Екатерина: 5 ошибок, из-за которых здоровое "
                 "питание не становится образом жизни",
    },
    {
        "date": "19.10.2026",
        "time": "11:00",
        "title": "Галеева Ирина: Упадок сил, выгорание или гормональный сбой? "
                 "Как понять, что происходит с организмом",
    },
    {
        "date": "19.10.2026",
        "time": "19:00",
        "title": "Галеева Ирина: Упадок сил, выгорание или гормональный сбой? "
                 "Как понять, что происходит с организмом",
    },
    {
        "date": "30.10.2026",
        "time": "11:00",
        "title": "Крумкач Ольга: Гормоны и энергия или женское здоровье "
                 "без мифов",
    },
    {
        "date": "30.10.2026",
        "time": "19:00",
        "title": "Крумкач Ольга: Гормоны и энергия или женское здоровье "
                 "без мифов",
    },
]

# Часовой пояс и время, в которое уходят напоминания «за столько-то дней».
# Пояс должен совпадать с поясом заданий на cron-job.org, которые будят сервис
# перед этим временем — иначе сервис проснётся уже после отправки.
TIMEZONE = pytz.timezone("Europe/Prague")
TIMEZONE_LABEL = "Прага"
REMINDER_HOUR = 12
REMINDER_MINUTE = 0

# За сколько дней до вебинара напоминать. Уберите лишние числа или добавьте
# свои: [7, 1] — за неделю и накануне. Пустой список [] — не напоминать вовсе.
# Все эти напоминания уходят в REMINDER_HOUR:REMINDER_MINUTE.
REMINDER_DAYS = [5, 3, 2, 1]

# Напоминать ли ещё и незадолго до начала.
# ВАЖНО: это работает, только если сервис в этот момент не спит. Какие
# будильники нужны на cron-job.org — показывает команда /stats.
REMINDER_BEFORE_START = True

# За сколько минут до начала отправлять это напоминание. Окно с запасом:
# если сервис проснулся где-то внутри него, напоминание всё равно уйдёт.
MINUTES_BEFORE_START = 75

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

# Показывается вместе с кнопками выбора вебинара
TEXT_REGISTER_PICK = "На какой вебинар вас записать?"

# {date}, {time} и {title} подставляются автоматически
TEXT_REGISTER_DONE = (
    "Готово! Записали вас на <b>{date}</b> в {time}\n\n"
    "<b>{title}</b>\n\n"
    "За день до вебинара пришлю напоминание."
)

TEXT_REGISTER_ALREADY = "Вы уже записаны на этот вебинар 🙂"

TEXT_REGISTER_GONE = (
    "Этот вебинар уже прошёл. Нажмите /register ещё раз, чтобы выбрать другой."
)

TEXT_REGISTER_FAILED = (
    "Не получилось вас записать. Попробуйте ещё раз чуть позже или напишите "
    "@AppolaAppola"
)

TEXT_NO_WEBINARS = (
    "Пока новых вебинаров не запланировано. Загляните чуть позже!"
)

# Заголовки в списке /courses
TEXT_COURSES_TITLE = "<b>Курсы и вебинары Tillo</b>"
TEXT_COURSES_UPCOMING = "<b>Ближайшие</b>"
TEXT_COURSES_PAST = "<b>Уже прошли</b>"

TEXT_QUESTIONS = "Напишите ваш вопрос — я передам его спикеру."

TEXT_QUESTIONS_DONE = "Спасибо! Ваш вопрос отправлен спикеру."

TEXT_HELP = (
    "Если что-то непонятно или появились вопросы про вебинар — "
    "пишите мне в личные сообщения @AppolaAppola"
)

TEXT_UNKNOWN = (
    "Нажмите кнопку <b>Меню</b> слева от поля ввода, чтобы выбрать действие."
)

# Шаблон напоминания. {when}, {title} и {time} подставляются автоматически.
TEXT_REMINDER = (
    "Привет! Напоминаем: {when} в {time} вебинар\n\n"
    "<b>{title}</b>\n\n"
    "Ждём вас 🙂"
)

# Чем заменяется {when} — зависит от того, за сколько до вебинара напоминаем
TEXT_WHEN_TOMORROW = "уже <b>завтра</b>"
TEXT_WHEN_DAYS = "<b>через {days}</b>"      # «через 5 дней»
TEXT_WHEN_SOON = "<b>уже совсем скоро</b>"

# ---------------------------------------------------------------------------
# Сообщения, которые бот пишет в рабочую группу (их видите только вы)
# ---------------------------------------------------------------------------

# {user} — имя человека, по которому можно нажать и написать ему лично,
# даже если у него нет @username.
TEXT_GROUP_REGISTRATION = (
    "✅ Регистрация на {date}\n"
    "{user} ({username})"
)

TEXT_GROUP_QUESTION = (
    "❓ Вопрос спикеру: {question}\n"
    "От: {user} ({username})"
)

TEXT_NO_USERNAME = "без @username"

# ---------------------------------------------------------------------------
# Личный ответ человеку (/dm) — команда для рабочей группы
# ---------------------------------------------------------------------------

TEXT_DM_NO_REPLY = (
    "Чтобы написать человеку лично, ответьте (reply) на сообщение бота "
    "«✅ Регистрация…» или «❓ Вопрос спикеру…» и напишите:\n"
    "/dm ваш текст"
)

TEXT_DM_NO_USER = (
    "В этом сообщении не видно, кому писать.\n\n"
    "Так бывает со старыми сообщениями — теми, что бот написал до обновления. "
    "Напишите <b>/who</b>: бот пришлёт свежий список всех записавшихся, и уже "
    "на него можно отвечать командой /dm."
)

TEXT_DM_NO_TEXT = (
    "Напишите текст после команды, например:\n/dm Спасибо за ваш вопрос!"
)

TEXT_DM_AMBIGUOUS = (
    "В этом сообщении сразу {count} человек — непонятно, кому из них писать.\n\n"
    "Ответьте на сообщение про <b>одного</b> человека: «✅ Регистрация…», "
    "«❓ Вопрос спикеру…» или карточку из /who."
)

TEXT_DM_SENT = "✅ Отправлено."

TEXT_DM_FAILED = "Не получилось отправить: {error}"

# ---------------------------------------------------------------------------
# Рассылка (/broadcast)
# ---------------------------------------------------------------------------

TEXT_BROADCAST_USAGE = (
    "Напишите текст после команды:\n\n"
    "<b>/broadcast</b> Привет! Вебинар уже завтра\n"
    "— всем, кто записан хоть на один вебинар\n\n"
    "<b>/broadcast 30.09.2026 19:00</b> Ссылка на зум: https://...\n"
    "— только тем, кто записан на этот вебинар. Так и надо слать ссылки: "
    "у каждого вебинара она своя.\n\n"
    "<b>/broadcast 30.09.2026</b> Привет!\n"
    "— всем в этот день; если вебинаров в нём несколько, бот попросит "
    "уточнить время."
)

TEXT_BROADCAST_PICK_TIME = (
    "На {date} несколько вебинаров. Укажите время — иначе сообщение "
    "(и ссылка на зум) уйдёт не тем:\n\n"
    "{items}\n\n"
    "Например:\n<code>/broadcast {example} Ссылка на зум: https://...</code>"
)

TEXT_BROADCAST_NO_WEBINAR = (
    "Вебинара {date} в списке нет. Проверьте дату — она пишется как "
    "ДД.ММ.ГГГГ, например 30.09.2026."
)

TEXT_BROADCAST_EMPTY_ONE = "На {date} пока никто не записан — рассылать некому."

TEXT_BROADCAST_EMPTY_ALL = "Пока никто не зарегистрирован — рассылать некому."

TEXT_BROADCAST_START_ALL = "Начинаю рассылку всем записанным — получателей: {count}"

TEXT_BROADCAST_START_ONE = (
    "Начинаю рассылку тем, кто записан на {date} — получателей: {count}"
)

TEXT_BROADCAST_DONE = "Готово. Доставлено: {sent}. Не доставлено: {failed}."

# ---------------------------------------------------------------------------
# Список записавшихся (/who)
# ---------------------------------------------------------------------------

TEXT_WHO_EMPTY = "Пока никто не записан."

# /who отвечает на вопрос «кто», а не «сколько» — считает /stats.
TEXT_WHO_HEADER = (
    "👥 <b>Карточки записавшихся{scope}</b>\n"
    "Чтобы написать человеку через бота — ответьте (reply) на его карточку "
    "командой /dm. По имени можно нажать, чтобы открыть профиль.\n"
    "<i>Сколько человек и сколько осталось места — это /stats.</i>"
)

TEXT_WHO_SCOPE = " на {date}"

# Карточка одного человека — ровно один человек, поэтому /dm не ошибётся
TEXT_WHO_CARD = "👤 {user}\n<i>записан(а) на {dates}</i>"

TEXT_WHO_TOO_MANY = (
    "Записавшихся уже {count} — столько карточек в чат не поместится.\n"
    "Укажите дату, например: <b>/who {example}</b>"
)

TEXT_WHO_NO_WEBINAR = "Вебинара {date} в списке нет."

TEXT_WHO_UNKNOWN_NAME = "без имени"

WHO_MAX = 15   # больше карточек за раз Телеграм не даст отправить подряд

# ---------------------------------------------------------------------------
# Инструкция для участников рабочей группы (/help внутри группы)
# ---------------------------------------------------------------------------

TEXT_ADMIN_HELP = (
    "🛠 <b>Команды в этой группе</b>\n\n"

    "<b>/stats</b> — <b>сколько</b>: человек на каждый вебинар, место в списке, "
    "какие напоминания уже ушли и когда уйдут следующие.\n"
    "<i>Когда нужно:</i> смотреть раз в день, пока идёт реклама.\n\n"

    "<b>/broadcast текст</b> — разослать сообщение всем, кто записан.\n"
    "<i>Пример:</i> /broadcast Завтра в 19:00 ждём вас на вебинаре!\n\n"

    "<b>/broadcast ДД.ММ.ГГГГ ЧЧ:ММ текст</b> — разослать только тем, кто "
    "записан на один конкретный вебинар. <b>Ссылки на зум шлите только так</b> "
    "— у каждого вебинара ссылка своя.\n"
    "<i>Пример:</i> /broadcast 30.09.2026 19:00 Ссылка на зум: https://...\n"
    "Если в этот день вебинаров несколько, а время вы не указали, бот "
    "переспросит и ничего не отправит.\n\n"

    "<b>/dm текст</b> — написать лично одному человеку. Сначала ответьте "
    "(reply) на сообщение «✅ Регистрация…» или «❓ Вопрос спикеру…», а потом "
    "напишите команду.\n"
    "<i>Пример:</i> /dm Спасибо за вопрос, передали спикеру!\n\n"

    "<b>/who</b> — <b>кто именно</b>: по карточке на каждого записавшегося, "
    "с именем, на которое можно нажать.\n"
    "<i>Когда нужно:</i> чтобы написать конкретному человеку — ответьте "
    "командой /dm на его карточку. Можно указать дату и время: "
    "/who 30.09.2026 19:00\n\n"

    "<b>/cleanup</b> — освободить место в списке, убрав записи на прошедшие "
    "вебинары. Сначала покажет, что именно удалит, и спросит подтверждение.\n"
    "<i>Когда нужно:</i> если /stats пишет, что список почти заполнен.\n\n"

    "<b>/check</b> — проверить вручную, не пора ли отправить напоминание.\n"
    "<i>Когда нужно:</i> обычно никогда — бот делает это сам в 12:00 "
    "накануне вебинара.\n\n"

    "<b>/id</b> — показать номер этого чата. Нужен только при настройке.\n\n"

    "———\n\n"

    "<b>Как написать человеку лично</b>\n"
    "Нажмите на его имя в сообщении «✅ Регистрация…» — откроется профиль, "
    "оттуда можно написать. Если у человека закрыты личные сообщения, "
    "ответьте на то же сообщение командой /dm — тогда напишет бот.\n\n"

    "<b>Как добавить вебинар</b>\n"
    "Вебинары и все тексты бота лежат в файле bot.py в самом верху. "
    "Дата строго в формате ДД.ММ.ГГГГ. Напоминание уходит само за день до "
    "вебинара — записывать его отдельно не нужно."
)

# {percent} — число, {left} — уже готовая фраза вида "53 регистрации"
TEXT_GROUP_CAPACITY = (
    "⚠️ Список записавшихся заполнен на {percent}%.\n"
    "Осталось место примерно на {left}. Когда оно закончится, новые "
    "регистрации перестанут сохраняться — сообщите разработчику заранее."
)

# Строчка про запас места в /stats
TEXT_STATS_CAPACITY = (
    "Место в списке: занято {percent}%, ещё примерно {left}."
)

TEXT_STATS_CAPACITY_WARN = (
    "⚠️ Список заполнен на {percent}% — осталось примерно {left}!"
)

TEXT_STATS_SCHEDULE = (
    "<b>Когда уходят напоминания</b>\n"
    "{days} — в {at} ({tz}){soon}\n"
    "Чтобы всё это ушло, сервис должен не спать в: {wake}. "
    "Это и есть список будильников на cron-job.org."
)

TEXT_STATS_SCHEDULE_SOON = "\nи ещё раз примерно за {minutes} минут до начала"

# Записи из старого формата, которые не привязаны к вебинару
TEXT_STATS_UNASSIGNED = (
    "⚠️ Не привязаны к вебинару (записаны до того, как в этот день появился "
    "второй вебинар — непонятно, на какой именно):\n{items}\n"
    "Напоминание им НЕ уйдёт. Попросите их записаться заново через /register."
)

TEXT_STATS_DUPLICATES = (
    "⚠️ Вебинары с одинаковой датой и временем: {items}\n"
    "Их невозможно различить — поменяйте время у одного из них."
)

# ---------------------------------------------------------------------------
# Очистка списка от прошедших вебинаров (/cleanup)
# ---------------------------------------------------------------------------

TEXT_CLEANUP_NOTHING = "Чистить нечего: записей на прошедшие вебинары нет."

TEXT_CLEANUP_PREVIEW = (
    "🧹 Можно освободить место, убрав записи на прошедшие вебинары:\n\n"
    "{items}\n\n"
    "Всего {removed}. Список станет заполнен на {after}% вместо {before}%.\n\n"
    "⚠️ Эти люди больше не получат рассылку через /broadcast. "
    "Кто и на что записывался, останется видно в сообщениях группы выше."
)

TEXT_CLEANUP_BTN_YES = "Удалить"
TEXT_CLEANUP_BTN_NO = "Отмена"

TEXT_CLEANUP_DONE = (
    "🧹 Готово, убрано {removed}.\n"
    "Список заполнен на {after}% вместо {before}%."
)

TEXT_CLEANUP_CANCELLED = "Отменено, ничего не удалено."

TEXT_CLEANUP_FAILED = (
    "Не получилось сохранить изменения — список остался как был."
)

TEXT_GROUP_REMINDER_SENT = (
    "🔔 Отправлено напоминание про вебинар {date}.\n"
    "Доставлено: {sent}, не доставлено: {failed}."
)


def courses_text() -> str:
    """Собирает список курсов из расписания выше, чтобы не дублировать вручную.

    Прошедшие вебинары показываем отдельным разделом: иначе /courses предлагал
    бы то, на что /register уже не даёт записаться.
    """
    today = today_local()
    upcoming, past = [], []
    for w in WEBINARS:
        d = parse_date(w["date"])
        if d and d < today:
            past.append((d, w))
        else:
            upcoming.append((d or datetime.date.max, w))
    upcoming.sort(key=lambda pair: pair[0])
    past.sort(key=lambda pair: pair[0], reverse=True)

    lines = [TEXT_COURSES_TITLE, ""]
    if upcoming:
        lines.append(TEXT_COURSES_UPCOMING)
        for i, (_, w) in enumerate(upcoming, start=1):
            lines.append(f"{i}. <b>{w['date']}</b> в {w['time']} — {clean_title(w)}")
        lines.append("")
    if past:
        lines.append(TEXT_COURSES_PAST)
        for _, w in past:
            lines.append(f"• <b>{w['date']}</b> в {w['time']} — {clean_title(w)}")
        lines.append("")
    if not upcoming and not past:
        lines.append(TEXT_NO_WEBINARS)
    return "\n".join(lines).strip()


def parse_time(value):
    """'19:00', '19.00', '1900' -> time(19, 0). None, если не разобрать."""
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    if len(digits) == 3:
        digits = "0" + digits
    if len(digits) != 4:
        return None
    hour, minute = int(digits[:2]), int(digits[2:])
    if hour > 23 or minute > 59:
        return None
    return datetime.time(hour, minute)


def parse_date(value: str):
    """'30.09.2026' -> date. Возвращает None, если формат неверный."""
    try:
        return datetime.datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError:
        logger.error("Неверный формат даты в расписании: %s", value)
        return None


def today_local() -> datetime.date:
    return datetime.datetime.now(TIMEZONE).date()


def upcoming_webinars(today: datetime.date = None):
    """Вебинары, которые ещё не прошли, по возрастанию даты."""
    if today is None:
        today = today_local()
    found = []
    for w in WEBINARS:
        d = parse_date(w["date"])
        if d and d >= today:
            found.append((d, w))
    found.sort(key=lambda pair: pair[0])
    return [w for _, w in found]


def webinar_key(w) -> str:
    """Уникальный ключ вебинара — дата и время: '30.09.2026-1900'.

    В один день может быть несколько вебинаров, поэтому одной даты мало.
    Время приводим к цифрам, чтобы '19:00', '19.00' и '19 00' давали один
    и тот же ключ, а не три разных.
    """
    return f"{w['date']}-{re.sub(r'[^0-9]', '', str(w.get('time', '')))}"


def key_label(key: str) -> str:
    """Ключ в человеческом виде: '30.09.2026-1900' -> '30.09.2026 в 19:00'."""
    w = find_webinar(key)
    if w:
        return f"{w['date']} в {w['time']}"
    date_part, marker, time_part = key.partition("-")
    if marker and len(time_part) == 4:
        return f"{date_part} в {time_part[:2]}:{time_part[2:]}"
    return key


def key_date(key: str):
    """Дата из ключа: '30.09.2026-1900' -> date(2026, 9, 30)."""
    return parse_date(key.split("-", 1)[0])


_LEADING_TIME = re.compile(r"^\s*\d{1,2}[.:\-]\d{2}\s+")


def clean_title(w) -> str:
    """Название без времени в начале — время бот показывает отдельно.

    Владелец может писать '11-00 Имя: тема', '11:00 Имя: тема' или просто
    'Имя: тема' — все три варианта выглядят одинаково.
    """
    return _LEADING_TIME.sub("", w["title"]).strip()


def find_webinar(key: str):
    """Вебинар по ключу. Для совместимости понимает и просто дату, если
    в этот день вебинар ровно один."""
    for w in WEBINARS:
        if webinar_key(w) == key:
            return w
    same_day = [w for w in WEBINARS if w["date"] == key]
    return same_day[0] if len(same_day) == 1 else None


def webinars_on(date_str: str):
    """Все вебинары этого дня, по времени."""
    found = [w for w in WEBINARS if w["date"] == date_str]
    found.sort(key=webinar_key)
    return found


def duplicate_keys():
    """Вебинары с одинаковой датой И временем — их различить невозможно."""
    seen, dupes = set(), []
    for w in WEBINARS:
        k = webinar_key(w)
        if k in seen and k not in dupes:
            dupes.append(k)
        seen.add(k)
    return dupes


# ============================================================================
# ХРАНИЛИЩЕ ПОДПИСЧИКОВ
# Бесплатный Render стирает память при перезапуске, поэтому список тех, кто
# записался, хранится в закреплённом сообщении внутри рабочей группы.
# Формат: по строке на каждый вебинар — "ДД.ММ.ГГГГ:id,id,id".
# ============================================================================

REGISTRY_HEADER = "📋 СПИСОК ЗАРЕГИСТРИРОВАННЫХ (не удалять, не редактировать)"

# дата вебинара -> множество id тех, кто на него записался
registrations = {}
reminded_keys = set()    # по каким вебинарам напоминание уже уходило
registry_message_id = None

# Удалось ли прочитать список при старте. Пока False — записывать НЕЛЬЗЯ:
# пустая память затрёт закреплённое сообщение со всеми регистрациями.
registry_loaded = False

# Закреплённое сообщение вмещает 4096 символов. Когда список упрётся в этот
# предел, новые регистрации перестанут сохраняться, поэтому бот предупреждает
# в группе заранее — на 80% и на 95% заполнения.
REGISTRY_LIMIT = 4096
CAPACITY_WARN_LEVELS = (80, 95)
CHARS_PER_REGISTRATION = 11   # ~10 цифр id плюс запятая

warned_levels = set()        # на каких порогах уже предупреждали

# Строка списка: "30.09.2026-1900:id,id" или старая "30.09.2026:id,id"
_KEY_LINE = re.compile(r"^(\d{2}\.\d{2}\.\d{4}(?:-\d{1,4})?):(.*)$")
_DATE_ONLY = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


def sent_tag(key: str, tag: str) -> str:
    """Отметка «это напоминание уже уходило»: 30.09.2026-1900@d1."""
    return f"{key}@{tag}"


def migrate_sent(entry: str) -> str:
    """Старая отметка без пометки — это было напоминание накануне."""
    key, marker, tag = entry.partition("@")
    return sent_tag(migrate_key(key), tag if marker else "d1")


def migrate_key(key: str) -> str:
    """Старая запись по одной дате -> ключ вебинара, если это однозначно.

    Пока вебинар в дне был один, список хранился просто по дате. Если в этот
    день вебинар по-прежнему один — переносим запись на него. Если их стало
    несколько, угадывать НЕЛЬЗЯ: человек записывался тогда, когда второго
    ещё не было, и определить какой именно он выбрал неоткуда. Такие записи
    остаются как есть, а /stats показывает их отдельно, чтобы они не пропали
    из виду.
    """
    if not _DATE_ONLY.match(key):
        return key
    same_day = webinars_on(key)
    if len(same_day) == 1:
        return webinar_key(same_day[0])
    if len(same_day) > 1:
        logger.error(
            "Запись за %s осталась непривязанной: в этот день %s вебинара, "
            "а старый формат не сохранял, на какой именно записался человек. "
            "Посмотрите /stats и перезапишите их вручную.",
            key, len(same_day),
        )
    return key


def unassigned_keys():
    """Кто из старых записей по одной дате остался без вебинара.

    Возвращает {дата: множество id}. Тех, кто уже записался на конкретный
    вебинар в этот же день, НЕ считаем: напоминание они получат, и жаловаться
    на них незачем — иначе предупреждение остаётся висеть после того, как люди
    перезаписались.
    """
    stranded = {}
    for key, ids in registrations.items():
        if not ids or not _DATE_ONLY.match(key):
            continue
        same_day = webinars_on(key)
        if len(same_day) <= 1:
            continue
        covered = set()
        for w in same_day:
            covered |= registrations.get(webinar_key(w), set())
        left = ids - covered
        if left:
            stranded[key] = left
    return stranded


def all_subscribers() -> set:
    """Все, кто записан хоть на один вебинар (без повторов)."""
    everyone = set()
    for ids in registrations.values():
        everyone |= ids
    return everyone


def registry_text() -> str:
    lines = [REGISTRY_HEADER, f"Всего: {len(all_subscribers())}"]
    for key in sorted(registrations,
                      key=lambda k: (key_date(k) or datetime.date.max, k)):
        ids = registrations[key]
        if ids:
            lines.append(f"{key}:" + ",".join(str(i) for i in sorted(ids)))
    lines.append("SENT:" + ",".join(sorted(reminded_keys)))
    if warned_levels:
        lines.append("WARN:" + ",".join(str(l) for l in sorted(warned_levels)))
    return "\n".join(lines)


def capacity_percent() -> int:
    """На сколько процентов заполнено закреплённое сообщение."""
    return min(100, round(len(registry_text()) * 100 / REGISTRY_LIMIT))


def capacity_left() -> int:
    """Сколько ещё регистраций примерно поместится."""
    free = REGISTRY_LIMIT - len(registry_text())
    return max(0, free // CHARS_PER_REGISTRATION)


def past_registration_dates(today: datetime.date = None):
    """Даты в списке, которые уже прошли: [(дата, сколько записей)]."""
    if today is None:
        today = today_local()
    found = []
    for key, ids in registrations.items():
        if not ids:
            continue
        d = key_date(key)
        if d and d < today:
            found.append((d, key, len(ids)))
    found.sort(key=lambda item: item[0])
    return [(date_str, count) for _, date_str, count in found]


def cleanup_frees(past) -> int:
    """Сколько символов освободит удаление этих дат (без изменения списка)."""
    freed = 0
    for date_str, _ in past:
        ids = registrations.get(date_str, set())
        line = f"{date_str}:" + ",".join(str(i) for i in sorted(ids))
        freed += len(line) + 1                # +1 за перевод строки
        for entry in reminded_keys:
            if entry.split("@", 1)[0] == date_str:
                freed += len(entry) + 1       # отметка в строке SENT: плюс запятая
    return freed


def plural_ru(number: int, one: str, few: str, many: str) -> str:
    """Русские числительные: 1 регистрацию, 2 регистрации, 5 регистраций."""
    if number % 100 in (11, 12, 13, 14):
        return many
    last = number % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many


def capacity_left_phrase() -> str:
    """'53 регистрации' — уже в нужном падеже, чтобы подставить в текст."""
    left = capacity_left()
    return f"{left} " + plural_ru(left, "регистрацию", "регистрации",
                                  "регистраций")


def load_registry(bot) -> bool:
    """Читает список подписчиков из закреплённого сообщения в группе.

    True — прочитали (в том числе «списка ещё нет, он пустой»).
    False — не смогли достучаться до группы; писать в этом состоянии нельзя.
    """
    global registry_message_id, registry_loaded

    if not ADMIN_CHAT_ID:
        logger.error(
            "ADMIN_CHAT_ID не задан! Регистрации НЕ сохраняются, уведомления "
            "в группу НЕ приходят, служебные команды отключены."
        )
        return False

    try:
        chat = bot.get_chat(ADMIN_CHAT_ID)
        pinned = chat.pinned_message
        if not (pinned and pinned.text and pinned.text.startswith(REGISTRY_HEADER)):
            logger.info("Закреплённого списка нет — будет создан при первой регистрации")
            registry_loaded = True
            return True

        registry_message_id = pinned.message_id
        # Разбираем по признаку строки, а не по её номеру: так порядок строк
        # и появление новых вебинаров ничего не ломают.
        for line in pinned.text.split("\n")[1:]:
            line = line.strip()
            if line.startswith("SENT:"):
                for d in line[5:].split(","):
                    if d.strip():
                        reminded_keys.add(migrate_sent(d.strip()))
                continue

            if line.startswith("WARN:"):
                for lvl in line[5:].split(","):
                    if lvl.strip().isdigit():
                        warned_levels.add(int(lvl.strip()))
                continue

            match = _KEY_LINE.match(line)
            if match:
                key, ids_part = match.group(1), match.group(2)
                ids = {int(p) for p in (x.strip() for x in ids_part.split(","))
                       if p.isdigit()}
                if ids:
                    registrations.setdefault(migrate_key(key), set()).update(ids)
                continue

            # Старый формат: голая строка из id без даты. Регистрации тогда были
            # общими, а не по вебинарам, поэтому переносить их некуда.
            if line and line.replace(",", "").isdigit():
                logger.warning(
                    "В закреплённом сообщении найден список в старом формате "
                    "(без дат) — он пропущен: %s", line[:80],
                )

        logger.info(
            "Загружено: вебинаров с записями %s, человек всего %s, "
            "отправленных напоминаний %s",
            len(registrations), len(all_subscribers()), len(reminded_keys),
        )
        registry_loaded = True
        return True
    except TelegramError as e:
        registry_loaded = False
        logger.error("Не удалось прочитать список подписчиков: %s", e)
        return False


def save_registry(bot) -> bool:
    """Обновляет закреплённое сообщение. True — сохранено (или сохранять некуда)."""
    global registry_message_id

    if not ADMIN_CHAT_ID:
        # Режим без группы (локальный запуск): сохранять негде, но и падать
        # незачем — про это уже написано ошибкой в лог при старте.
        return True

    # Если при старте список прочитать не удалось (сеть моргнула на холодном
    # старте), в памяти он пустой. Записать его сейчас — значит стереть всех,
    # кто уже был записан. Поэтому сначала пробуем перечитать: load_registry
    # ДОПОЛНЯЕТ память, а не заменяет её, поэтому свежая регистрация не теряется.
    if not registry_loaded:
        logger.error("Список не был прочитан при старте — перечитываю, "
                     "чтобы не затереть существующие регистрации")
        if not load_registry(bot):
            logger.error("Список по-прежнему недоступен — НИЧЕГО не пишу")
            return False

    # Пороги, которые пройдены впервые. Считаем ДО записи, чтобы отметка
    # "предупреждали" сохранилась тем же самым сообщением, а не следующим.
    percent = capacity_percent()
    warned_levels.difference_update({l for l in list(warned_levels) if percent < l})
    pending = [l for l in CAPACITY_WARN_LEVELS
               if l not in warned_levels and percent >= l]
    warned_levels.update(pending)

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
        # Не сохранилось — значит и про предупреждение отмечать нечего
        warned_levels.difference_update(pending)
        logger.error("Не удалось сохранить список подписчиков: %s", e)
        return False

    for level in pending:
        logger.warning("Список заполнен на %s%% — предупреждаю группу", percent)
        notify_group(bot, TEXT_GROUP_CAPACITY.format(
            percent=percent, left=capacity_left_phrase(),
        ))
    return True


def user_link(user) -> str:
    """Имя человека ссылкой на его профиль.

    Обычный аккаунт не может написать по числовому id — нужен либо @username,
    либо вот такая ссылка. Телеграм гарантирует, что она работает для тех, кто
    уже писал боту, а это все, кто регистрировался.
    """
    return (f'<a href="tg://user?id={user.id}">'
            f'{html.escape(user.full_name)}</a>')


def user_handle(user) -> str:
    return f"@{html.escape(user.username)}" if user.username else TEXT_NO_USERNAME


def notify_group(bot, text: str) -> None:
    """Пишет в рабочую группу. Молча не теряем — при ошибке шумим в лог."""
    if not ADMIN_CHAT_ID:
        return
    try:
        bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode='HTML')
    except TelegramError as e:
        logger.error(
            "Не удалось написать в группу %s: %s. Проверьте ADMIN_CHAT_ID "
            "и что бот добавлен в группу.", ADMIN_CHAT_ID, e,
        )


def is_admin_chat(update: Update) -> bool:
    """Служебные команды работают только внутри рабочей группы."""
    return bool(ADMIN_CHAT_ID) and update.effective_chat.id == ADMIN_CHAT_ID


# Ошибки, после которых человека действительно незачем держать в списке:
# он заблокировал бота или удалил аккаунт. Всё остальное — таймаут, сбой сети,
# лимит Телеграма, лишний тег в тексте рассылки — временное, и вычёркивать
# человека из-за этого нельзя: он просто не получит одну рассылку.
_PERMANENT_FAILURES = (
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "peer_id_invalid",
    "bot can't initiate conversation",
)


def is_permanent_failure(error) -> bool:
    """Человек ушёл навсегда (True) или это временный сбой (False)?"""
    if isinstance(error, Unauthorized):
        return True
    if isinstance(error, BadRequest):
        text = str(error).lower()
        return any(marker in text for marker in _PERMANENT_FAILURES)
    return False


def broadcast(bot, text: str, recipients):
    """Рассылает текст указанным людям. Возвращает (доставлено, ошибок)."""
    sent, failed, gone = 0, 0, []
    for user_id in sorted(recipients):
        try:
            bot.send_message(chat_id=user_id, text=text, parse_mode='HTML')
            sent += 1
        except TelegramError as e:
            failed += 1
            if is_permanent_failure(e):
                logger.info("Убираю %s из списка — адресат недоступен: %s",
                            user_id, e)
                gone.append(user_id)
            else:
                logger.warning("Не доставлено %s (временная ошибка, оставляю "
                               "в списке): %s", user_id, e)
        time.sleep(0.05)  # чтобы не упереться в лимиты Телеграма

    if gone:
        for ids in registrations.values():
            for user_id in gone:
                ids.discard(user_id)
        save_registry(bot)
    return sent, failed


# ============================================================================
# АВТОМАТИЧЕСКИЕ НАПОМИНАНИЯ
# ============================================================================


def send_reminders(bot, today: datetime.date = None) -> int:
    """Проверяет, есть ли вебинар завтра, и если да — рассылает напоминание."""
    if today is None:
        today = today_local()

    total = 0
    for w in WEBINARS:
        webinar_date = parse_date(w["date"])
        if webinar_date is None:
            continue
        days_left = (webinar_date - today).days
        if days_left not in REMINDER_DAYS:
            continue

        key = webinar_key(w)
        tag = f"d{days_left}"
        if sent_tag(key, tag) in reminded_keys:
            logger.info("Напоминание про %s за %s дн. уже отправляли",
                        key, days_left)
            continue

        recipients = registrations.get(key, set())
        if not recipients:
            # Намеренно НЕ помечаем как отправленное: иначе /stats покажет
            # "напоминание уже отправлено" там, где не ушло ничего, а тот, кто
            # запишется позже в тот же день, уже ничего не получит даже по
            # ручной команде /check.
            logger.info("На вебинар %s никто не записался — напоминать некому",
                        key)
            continue

        text = TEXT_REMINDER.format(when=when_phrase(days_left),
                                    title=clean_title(w), time=w["time"])
        sent, failed = broadcast(bot, text, recipients)
        reminded_keys.add(sent_tag(key, tag))
        save_registry(bot)
        total += sent

        logger.info("Напоминание про %s за %s дн.: доставлено %s, ошибок %s",
                    key, days_left, sent, failed)
        notify_group(bot, TEXT_GROUP_REMINDER_SENT.format(
            date=f"{w['date']} в {w['time']} ({when_phrase(days_left)})",
            sent=sent, failed=failed,
        ))
    return total


def when_phrase(days_left: int) -> str:
    """«завтра» / «через 5 дней» — в {when} шаблона напоминания."""
    if days_left <= 1:
        return TEXT_WHEN_TOMORROW
    return TEXT_WHEN_DAYS.format(
        days=f"{days_left} " + plural_ru(days_left, "день", "дня", "дней"))


def send_start_reminders(bot, now: datetime.datetime = None) -> int:
    """Напоминание незадолго до начала вебинара.

    Проверяем окном, а не точной минутой: бесплатный сервис спит и может
    проснуться в любой момент внутри окна — напоминание всё равно уйдёт,
    и ровно один раз.
    """
    if now is None:
        now = datetime.datetime.now(TIMEZONE)

    total = 0
    for w in WEBINARS:
        webinar_date = parse_date(w["date"])
        start_time = parse_time(w.get("time"))
        if webinar_date is None or start_time is None:
            continue

        start = TIMEZONE.localize(
            datetime.datetime.combine(webinar_date, start_time))
        minutes_left = (start - now).total_seconds() / 60
        if not 0 < minutes_left <= MINUTES_BEFORE_START:
            continue

        key = webinar_key(w)
        if sent_tag(key, "soon") in reminded_keys:
            continue

        recipients = registrations.get(key, set())
        if not recipients:
            logger.info("На вебинар %s никто не записался — напоминать некому",
                        key)
            continue

        text = TEXT_REMINDER.format(when=TEXT_WHEN_SOON,
                                    title=clean_title(w), time=w["time"])
        sent, failed = broadcast(bot, text, recipients)
        reminded_keys.add(sent_tag(key, "soon"))
        save_registry(bot)
        total += sent

        logger.info("Напоминание перед началом %s: доставлено %s, ошибок %s",
                    key, sent, failed)
        notify_group(bot, TEXT_GROUP_REMINDER_SENT.format(
            date=f"{w['date']} в {w['time']} (перед началом)",
            sent=sent, failed=failed,
        ))
    return total


def daily_job(context: CallbackContext) -> None:
    send_reminders(context.bot)


def soon_job(context: CallbackContext) -> None:
    """Пока сервис не спит, каждые несколько минут проверяем, не начинается ли
    вебинар совсем скоро. Так напоминание уйдёт, даже если сервис проснулся
    не ровно в нужную минуту."""
    if REMINDER_BEFORE_START:
        send_start_reminders(context.bot)


def wakeup_times():
    """Во сколько сервис обязан не спать, чтобы напоминания ушли.

    Это и есть список будильников для cron-job.org: время напоминаний «за
    столько-то дней» плюс по одному перед началом каждого вебинара.
    """
    needed = set()
    if REMINDER_DAYS:
        needed.add(f"{REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d}")
    if REMINDER_BEFORE_START:
        for w in WEBINARS:
            start = parse_time(w.get("time"))
            if start is None:
                continue
            # Будильник ставим чуть ПОЗЖЕ начала окна, а не раньше: сервис
            # просыпается примерно на 15 минут, и эти 15 минут должны целиком
            # попасть внутрь окна. Если будить до его открытия, на проверки
            # внутри окна остаётся всего несколько минут.
            moment = (datetime.datetime.combine(datetime.date(2000, 1, 1), start)
                      - datetime.timedelta(minutes=MINUTES_BEFORE_START - 5))
            needed.add(moment.strftime("%H:%M"))
    return sorted(needed)


# ============================================================================
# КОМАНДЫ ДЛЯ ПОЛЬЗОВАТЕЛЕЙ
# ============================================================================


def start(update: Update, context: CallbackContext) -> None:
    context.user_data['state'] = None
    update.message.reply_text(TEXT_START, parse_mode='HTML')


def button_label(w) -> str:
    """Подпись на кнопке: дата, время и имя спикера.

    Время обязательно — в один день вебинаров может быть несколько, и без
    него кнопки выглядели бы одинаково.
    """
    short = clean_title(w).split(":")[0].strip()
    if len(short) > 26:
        short = short[:25].rstrip() + "…"
    return f"{w['date']} {w['time']} — {short}"


def register(update: Update, context: CallbackContext) -> None:
    context.user_data['state'] = None
    upcoming = upcoming_webinars()
    if not upcoming:
        update.message.reply_text(TEXT_NO_WEBINARS, parse_mode='HTML')
        return

    keyboard = [
        [InlineKeyboardButton(button_label(w), callback_data=f"reg:{webinar_key(w)}")]
        for w in upcoming
    ]
    update.message.reply_text(
        TEXT_REGISTER_PICK,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def register_callback(update: Update, context: CallbackContext) -> None:
    """Нажатие на кнопку выбора вебинара — здесь и происходит регистрация."""
    query = update.callback_query
    query.answer()   # без этого кнопка «крутится» у пользователя

    key = query.data.partition(":")[2]
    user = query.from_user
    w = find_webinar(key)
    webinar_date = key_date(key) if w else None

    # Кнопку могли нажать на старом сообщении — вебинара может уже не быть
    if w is None or webinar_date is None or webinar_date < today_local():
        query.edit_message_text(TEXT_REGISTER_GONE, parse_mode='HTML')
        return

    already = registrations.get(key, set())
    if user.id in already:
        query.edit_message_text(TEXT_REGISTER_ALREADY, parse_mode='HTML')
        return

    # Сначала записываем и сохраняем, и только потом подтверждаем человеку —
    # иначе можно сказать «готово» там, где на самом деле ничего не сохранилось.
    registrations.setdefault(key, set()).add(user.id)
    if not save_registry(context.bot):
        registrations[key].discard(user.id)
        query.edit_message_text(TEXT_REGISTER_FAILED, parse_mode='HTML')
        return

    query.edit_message_text(
        TEXT_REGISTER_DONE.format(date=w["date"], time=w["time"],
                                  title=clean_title(w)),
        parse_mode='HTML',
    )
    notify_group(context.bot, TEXT_GROUP_REGISTRATION.format(
        date=f"{w['date']} в {w['time']}",
        user=user_link(user), username=user_handle(user),
    ))


def questions(update: Update, context: CallbackContext) -> None:
    context.user_data['state'] = 'QUESTIONS'
    update.message.reply_text(TEXT_QUESTIONS, parse_mode='HTML')


def help_command(update: Update, context: CallbackContext) -> None:
    # В рабочей группе /help показывает инструкцию по служебным командам,
    # в личке — обычную помощь для участников вебинаров.
    if is_admin_chat(update):
        update.message.reply_text(TEXT_ADMIN_HELP, parse_mode='HTML')
        return
    if update.effective_chat.type != "private":
        return
    update.message.reply_text(TEXT_HELP, parse_mode='HTML')


def courses(update: Update, context: CallbackContext) -> None:
    update.message.reply_text(courses_text(), parse_mode='HTML')


def handle_message(update: Update, context: CallbackContext) -> None:
    # В группе бот на обычные сообщения не реагирует
    if update.effective_chat.type != "private":
        return

    state = context.user_data.get('state')
    user = update.effective_user

    if state == 'QUESTIONS':
        update.message.reply_text(TEXT_QUESTIONS_DONE, parse_mode='HTML')
        notify_group(context.bot, TEXT_GROUP_QUESTION.format(
            # текст пользователя экранируем: сообщение уходит как HTML
            question=html.escape(update.message.text),
            user=user_link(user),
            username=user_handle(user),
        ))
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

    today = today_local()
    lines = []
    for w in upcoming_webinars(today):
        d = parse_date(w["date"])
        days = (d - today).days
        key = webinar_key(w)
        count = len(registrations.get(key, set()))
        done = sorted(e.split("@", 1)[1] for e in reminded_keys
                      if e.split("@", 1)[0] == key)
        mark = f" (напоминаний отправлено: {len(done)})" if done else ""
        lines.append(f"• {w['date']} в {w['time']} — через {days} дн. — "
                     f"записано {count} чел.{mark}")

    text = f"Всего зарегистрировано: {len(all_subscribers())} чел.\n\n"
    text += "Ближайшие вебинары:\n" + ("\n".join(lines) if lines
                                       else "— расписание пустое")
    text += "\n\n" + TEXT_STATS_SCHEDULE.format(
        days=(", ".join(f"за {d} " + plural_ru(d, "день", "дня", "дней")
                        for d in sorted(REMINDER_DAYS, reverse=True))
              or "— не настроены"),
        at=f"{REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d}",
        tz=TIMEZONE_LABEL,
        soon=(TEXT_STATS_SCHEDULE_SOON.format(minutes=MINUTES_BEFORE_START)
              if REMINDER_BEFORE_START else ""),
        wake=", ".join(wakeup_times()) or "—",
    )

    percent = capacity_percent()
    template = (TEXT_STATS_CAPACITY_WARN if percent >= CAPACITY_WARN_LEVELS[0]
                else TEXT_STATS_CAPACITY)
    text += "\n\n" + template.format(percent=percent,
                                      left=capacity_left_phrase())

    # Записи, которые не удалось привязать к конкретному вебинару, иначе бы
    # они молча не получили напоминание.
    stranded = unassigned_keys()
    if stranded:
        text += "\n\n" + TEXT_STATS_UNASSIGNED.format(items="\n".join(
            f"• {key_label(k)} — {records_phrase(len(stranded[k]))}"
            for k in sorted(stranded)))

    dupes = duplicate_keys()
    if dupes:
        text += "\n\n" + TEXT_STATS_DUPLICATES.format(
            items=", ".join(key_label(k) for k in dupes))

    update.message.reply_text(text, parse_mode='HTML')


_name_cache = {}


def person_link(bot, user_id: int) -> str:
    """Имя человека ссылкой. Имя спрашиваем у Телеграма и запоминаем."""
    name = _name_cache.get(user_id)
    if name is None:
        try:
            chat = bot.get_chat(user_id)
            name = (chat.full_name or "").strip() or f"id {user_id}"
            if chat.username:
                name = f"{name} (@{chat.username})"
        except TelegramError as e:
            # Имя узнать не вышло — ссылка всё равно рабочая, она строится
            # из id, который у нас уже есть.
            logger.info("Не удалось узнать имя %s: %s", user_id, e)
            name = f"{TEXT_WHO_UNKNOWN_NAME} · id {user_id}"
        _name_cache[user_id] = name
    return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'


_TIME_ONLY = re.compile(r"^\d{1,2}[:.]\d{2}$")


def split_target(text: str):
    """'30.09.2026 19:00 текст' -> ('30.09.2026', '19:00', 'текст').

    Дата и время необязательны; что не распознали — остаётся текстом.
    """
    date_str = time_str = ""
    first, _, tail = text.partition(' ')
    if _DATE_ONLY.match(first):
        date_str, text = first, tail.strip()
        second, _, tail2 = text.partition(' ')
        if _TIME_ONLY.match(second):
            time_str, text = second, tail2.strip()
    return date_str, time_str, text


def keys_for(wanted: str):
    """Ключи вебинаров по дате (все в этот день) или по точному ключу."""
    if find_webinar(wanted) and not _DATE_ONLY.match(wanted):
        return [wanted]
    keys = [webinar_key(w) for w in webinars_on(wanted)]
    # запись из старого формата под самой датой — её тоже учитываем
    if wanted in registrations and wanted not in keys:
        keys.append(wanted)
    return keys


def who_command(update: Update, context: CallbackContext) -> None:
    """/who [ДД.ММ.ГГГГ] — свежий список записавшихся, по именам можно нажимать."""
    if not is_admin_chat(update):
        return

    w_date, w_time, _extra = split_target(
        update.message.text.partition(' ')[2].strip())
    wanted = f"{w_date}-{re.sub(r'[^0-9]', '', w_time)}" if w_time else w_date
    if wanted:
        dates = keys_for(wanted)
        if not dates:
            update.message.reply_text(TEXT_WHO_NO_WEBINAR.format(date=wanted))
            return
        if not any(registrations.get(k) for k in dates):
            update.message.reply_text(
                TEXT_BROADCAST_EMPTY_ONE.format(date=wanted))
            return
    else:
        dates = sorted(
            (k for k, ids in registrations.items() if ids),
            key=lambda k: (key_date(k) or datetime.date.max, k),
        )
    if not dates:
        update.message.reply_text(TEXT_WHO_EMPTY)
        return

    total = sum(len(registrations.get(d, ())) for d in dates)
    if total > WHO_MAX:
        update.message.reply_text(
            TEXT_WHO_TOO_MANY.format(count=total, example=dates[0]),
            parse_mode='HTML')
        return

    update.message.reply_text(
        TEXT_WHO_HEADER.format(
            scope=TEXT_WHO_SCOPE.format(date=wanted) if wanted else ""),
        parse_mode='HTML', disable_web_page_preview=True)

    # По карточке на человека: в каждой ровно один, поэтому ответ командой
    # /dm всегда однозначен.
    on_dates = {}
    for date_str in dates:
        for uid in registrations.get(date_str, set()):
            on_dates.setdefault(uid, []).append(date_str)

    for uid in sorted(on_dates, key=lambda u: person_link(context.bot, u)):
        try:
            update.message.reply_text(
                TEXT_WHO_CARD.format(
                    user=person_link(context.bot, uid),
                    dates=", ".join(key_label(k) for k in sorted(
                        on_dates[uid],
                        key=lambda k: (key_date(k) or datetime.date.max, k))),
                ),
                parse_mode='HTML', disable_web_page_preview=True)
        except TelegramError as e:
            logger.error("Не удалось отправить карточку %s: %s", uid, e)
            break
        time.sleep(0.3)   # лимит Телеграма на сообщения в группу


def records_phrase(count: int) -> str:
    """'12 записей' — в нужном падеже."""
    return f"{count} " + plural_ru(count, "запись", "записи", "записей")


def cleanup_command(update: Update, context: CallbackContext) -> None:
    """/cleanup — показать, сколько места занимают прошедшие вебинары."""
    if not is_admin_chat(update):
        return

    past = past_registration_dates()
    if not past:
        update.message.reply_text(TEXT_CLEANUP_NOTHING)
        return

    items = "\n".join(f"• {key_label(date_str)} — {records_phrase(count)}"
                      for date_str, count in past)
    before = capacity_percent()
    after = max(0, round((len(registry_text()) - cleanup_frees(past)) * 100
                         / REGISTRY_LIMIT))
    keyboard = [[
        InlineKeyboardButton(TEXT_CLEANUP_BTN_YES, callback_data="cleanup:yes"),
        InlineKeyboardButton(TEXT_CLEANUP_BTN_NO, callback_data="cleanup:no"),
    ]]
    update.message.reply_text(
        TEXT_CLEANUP_PREVIEW.format(
            items=items,
            removed=records_phrase(sum(c for _, c in past)),
            before=before, after=after,
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def cleanup_callback(update: Update, context: CallbackContext) -> None:
    """Кнопки под /cleanup. Работают только внутри рабочей группы."""
    query = update.callback_query
    query.answer()

    if not (ADMIN_CHAT_ID and query.message
            and query.message.chat.id == ADMIN_CHAT_ID):
        return

    if query.data == "cleanup:no":
        query.edit_message_text(TEXT_CLEANUP_CANCELLED)
        return

    # Пересчитываем на момент нажатия: между показом и нажатием мог
    # смениться день.
    past = past_registration_dates()
    if not past:
        query.edit_message_text(TEXT_CLEANUP_NOTHING)
        return

    before = capacity_percent()
    removed = sum(count for _, count in past)
    backup = {d: set(registrations[d]) for d, _ in past if d in registrations}
    backup_sent = set(reminded_keys)

    for date_str, _ in past:
        registrations.pop(date_str, None)
        for entry in [e for e in reminded_keys
                      if e.split("@", 1)[0] == date_str]:
            reminded_keys.discard(entry)

    if not save_registry(context.bot):
        registrations.update(backup)
        reminded_keys.clear()
        reminded_keys.update(backup_sent)
        query.edit_message_text(TEXT_CLEANUP_FAILED)
        return

    logger.info("Очистка: убрано %s записей за %s прошедших вебинаров",
                removed, len(past))
    query.edit_message_text(TEXT_CLEANUP_DONE.format(
        removed=records_phrase(removed),
        before=before, after=capacity_percent(),
    ))


def replied_user_ids(message):
    """Все id людей, упомянутых в сообщении, на которое ответили.

    Возвращаем ВСЕХ, а не первого: если в сообщении несколько человек, брать
    первого попавшегося нельзя — личное сообщение молча уйдёт не тому.

    По @username id узнать нельзя: getChat принимает @имя только для каналов и
    супергрупп, но не для людей. Поэтому в сообщениях, написанных до появления
    ссылок, адресата взять неоткуда — для них есть команда /who.
    """
    found = []
    for entity in (message.entities or []):
        user_id = 0
        if entity.type == MessageEntity.TEXT_MENTION and entity.user:
            user_id = entity.user.id
        elif entity.type == MessageEntity.TEXT_LINK and entity.url:
            match = re.search(r"tg://user\?id=(\d+)", entity.url)
            if match:
                user_id = int(match.group(1))
        if user_id and user_id not in found:
            found.append(user_id)
    return found


def dm_command(update: Update, context: CallbackContext) -> None:
    """/dm текст — написать лично тому, на чьё сообщение ответили."""
    if not is_admin_chat(update):
        return

    replied = update.message.reply_to_message
    if not replied:
        update.message.reply_text(TEXT_DM_NO_REPLY, parse_mode='HTML')
        return

    people = replied_user_ids(replied)
    if not people:
        update.message.reply_text(TEXT_DM_NO_USER, parse_mode='HTML')
        return
    if len(people) > 1:
        # Ни в коем случае не угадываем: личное сообщение ушло бы не тому.
        update.message.reply_text(
            TEXT_DM_AMBIGUOUS.format(count=len(people)), parse_mode='HTML')
        return
    user_id = people[0]

    text = update.message.text.partition(' ')[2].strip()
    if not text:
        update.message.reply_text(TEXT_DM_NO_TEXT, parse_mode='HTML')
        return

    try:
        context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML')
    except TelegramError as e:
        logger.error("Не удалось написать лично %s: %s", user_id, e)
        update.message.reply_text(TEXT_DM_FAILED.format(error=e))
        return

    update.message.reply_text(TEXT_DM_SENT)


def broadcast_command(update: Update, context: CallbackContext) -> None:
    """/broadcast текст — разослать сообщение всем вручную."""
    if not is_admin_chat(update):
        return

    # Первыми словами можно указать дату и время — тогда рассылка уйдёт
    # только тем, кто записан именно на этот вебинар.
    date_str, time_str, rest = split_target(
        update.message.text.partition(' ')[2].strip())

    if not rest:
        update.message.reply_text(TEXT_BROADCAST_USAGE, parse_mode='HTML')
        return

    if date_str:
        same_day = webinars_on(date_str)
        if time_str:
            key = f"{date_str}-{re.sub(r'[^0-9]', '', time_str)}"
            if not find_webinar(key) and key not in registrations:
                update.message.reply_text(TEXT_BROADCAST_NO_WEBINAR.format(
                    date=f"{date_str} в {time_str}"))
                return
            keys = [key]
        elif len(same_day) > 1:
            # В рассылке почти всегда ссылка на зум, а она у каждого вебинара
            # своя. Угадывать нельзя — просим уточнить время.
            update.message.reply_text(
                TEXT_BROADCAST_PICK_TIME.format(
                    date=date_str,
                    items="\n".join(
                        f"• <code>/broadcast {date_str} {w['time']}</code> — "
                        f"записано "
                        f"{len(registrations.get(webinar_key(w), set()))} чел."
                        for w in same_day),
                    example=f"{date_str} {same_day[0]['time']}",
                ), parse_mode='HTML')
            return
        else:
            keys = keys_for(date_str)
            if not keys:
                update.message.reply_text(
                    TEXT_BROADCAST_NO_WEBINAR.format(date=date_str))
                return
        recipients = set()
        for key in keys:
            recipients |= registrations.get(key, set())
        if not recipients:
            update.message.reply_text(
                TEXT_BROADCAST_EMPTY_ONE.format(date=date_str))
            return
        update.message.reply_text(TEXT_BROADCAST_START_ONE.format(
            date=f"{date_str} в {time_str}" if time_str else date_str,
            count=len(recipients)))
    else:
        recipients = all_subscribers()
        if not recipients:
            update.message.reply_text(TEXT_BROADCAST_EMPTY_ALL)
            return
        update.message.reply_text(TEXT_BROADCAST_START_ALL.format(
            count=len(recipients)))

    sent, failed = broadcast(context.bot, rest, recipients)
    update.message.reply_text(TEXT_BROADCAST_DONE.format(sent=sent, failed=failed))


def check_command(update: Update, context: CallbackContext) -> None:
    """/check — вручную запустить проверку напоминаний (для теста)."""
    if not is_admin_chat(update):
        return
    total = send_reminders(context.bot)
    if REMINDER_BEFORE_START:
        total += send_start_reminders(context.bot)
    if total == 0:
        update.message.reply_text(
            "Проверила: на завтра вебинаров нет (или напоминание уже отправлено)."
        )


# ============================================================================
# HEALTH-СТРАНИЦА
# Нужна, чтобы cron-job.org будил сервис и получал ответ 200, а не ошибку.
# ============================================================================


class HealthHandler(tornado.web.RequestHandler):
    """Страница для будильников cron-job.org.

    Отвечаем максимально скучно: обычный текст, два байта, без ETag и без
    кэширования. Будильнику нужен только код 200 — всё остальное лишний повод
    для ошибок на его стороне.
    """

    SUPPORTED_METHODS = ["GET", "HEAD"]

    def compute_etag(self):
        return None      # без ETag не будет и ответов 304 на If-None-Match

    def set_default_headers(self) -> None:
        self.set_header("Content-Type", "text/plain; charset=utf-8")
        self.set_header("Cache-Control", "no-store")

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
    dispatcher.add_handler(CommandHandler('cleanup', cleanup_command))
    dispatcher.add_handler(CommandHandler('dm', dm_command))
    dispatcher.add_handler(CommandHandler('who', who_command))
    dispatcher.add_handler(CallbackQueryHandler(register_callback, pattern=r'^reg:'))
    dispatcher.add_handler(
        CallbackQueryHandler(cleanup_callback, pattern=r'^cleanup:')
    )
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

    dupes = duplicate_keys()
    if dupes:
        logger.error("Вебинары с одинаковой датой и временем: %s — их нельзя "
                     "различить, поменяйте время у одного из них", dupes)

    load_registry(updater.bot)

    # Ежедневная проверка: не начинается ли вебинар завтра
    updater.job_queue.run_daily(
        daily_job,
        time=datetime.time(hour=REMINDER_HOUR, minute=REMINDER_MINUTE,
                           tzinfo=TIMEZONE),
    )
    logger.info("Ежедневная проверка напоминаний запланирована на %02d:%02d (%s)",
                REMINDER_HOUR, REMINDER_MINUTE, TIMEZONE_LABEL)

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
