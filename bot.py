import csv
import datetime
import html
import io
import logging
import os
import re
import threading
import time
import zipfile

import pytz
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
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
from telegram.error import TelegramError, Unauthorized, BadRequest, RetryAfter

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ID группы, куда падают регистрации и вопросы. Задаётся в настройках Render.
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))

# ============================================================================
# РАСПИСАНИЕ ВЕБИНАРОВ
# Добавляйте и меняйте строки здесь. Дата строго в формате ДД.ММ.ГГГГ,
# время — ЧЧ:ММ по МОСКВЕ (пояс задан ниже, в WEBINAR_TIMEZONE).
# Напоминания тем, кто записался именно на этот вебинар, бот разошлёт сам.
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
# Пояс должен совпадать с поясом будильников, которые будят сервис
# перед этим временем — иначе сервис проснётся уже после отправки.
TIMEZONE = pytz.timezone("Europe/Prague")
TIMEZONE_LABEL = "Прага"
REMINDER_HOUR = 12
REMINDER_MINUTE = 0

# В каком поясе записано время вебинаров в WEBINARS. От него считается
# напоминание «перед началом». Это НЕ то же, что TIMEZONE выше: тот задаёт,
# когда уходят напоминания «за N дней», и должен совпадать с будильниками.
# В Москве нет перехода на зимнее время, а в Праге есть, поэтому разница
# между ними — час летом и два часа зимой. Бот учитывает это сам.
WEBINAR_TIMEZONE = pytz.timezone("Europe/Moscow")
WEBINAR_TIMEZONE_LABEL = "МСК"

# За сколько дней до вебинара напоминать. Уберите лишние числа или добавьте
# свои: [7, 1] — за неделю и накануне. Пустой список [] — не напоминать вовсе.
# Все эти напоминания уходят в REMINDER_HOUR:REMINDER_MINUTE.
REMINDER_DAYS = [5, 3, 2, 1]

# Напоминать ли ещё и незадолго до начала.
# ВАЖНО: это работает, только если сервис в этот момент не спит. Какие
# будильники нужны и во сколько — показывает команда /stats.
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

# Шаблон напоминания — один на все вебинары. Бот сам подставляет:
#   {when}        «через 5 дней», «уже завтра» или «уже совсем скоро»
#   {time}        время вебинара, {title} — его название
#   {speaker}     имя спикера — часть названия до первого двоеточия
#                 («Черепанова Екатерина»). Пишите так, чтобы имя стояло
#                 в начальной форме: «{speaker} ответит», а не «для {speaker}».
#   {other_times} абзац TEXT_REMINDER_OTHER_TIMES (он ниже) — или ничего
# В фигурных скобках — только эти слова. Если там ошибка, напоминание всё
# равно уйдёт, в упрощённом виде, а бот напишет об ошибке в группу.
TEXT_REMINDER = (
    "Привет! Напоминаем: {when} в {time} вебинар\n\n"
    "<b>{title}</b>\n\n"
    "Ждём вас 🙂"
    "{other_times}\n\n"
    "А еще мы через бот собираем вопросы — {speaker} ответит на них на "
    "вебинаре. Есть вопрос - бегите в меню бота, жмите 'Вопросы для спикера' "
    "и присылайте свой вопрос."
)

# Встаёт на место {other_times}, только если в этот день несколько сеансов,
# и только в напоминаниях «за N дней» — в «скоро» его нет: другой сеанс
# к тому времени может уже пройти. {date} — «30 сентября»,
# {times} — «11:00 или 19:00».
TEXT_REMINDER_OTHER_TIMES = (
    "\n\nНапоминаем, что вебинар {date} пройдет в {times} по московскому "
    "времени. Это один и тот же вебинар, просто в разное время — для вашего "
    "удобства.\n\n"
    "Если вы хотите прийти в другое время — просто зайдите в меню бота и "
    "зарегистрируйтесь еще раз."
)

# Что встанет на место {speaker}, если в названии нет двоеточия
TEXT_SPEAKER_FALLBACK = "спикер"

# Запасной текст: уходит, если в TEXT_REMINDER ошибка в фигурных скобках.
# Здесь можно использовать только {when}, {time} и {title}.
TEXT_REMINDER_FALLBACK = (
    "Привет! Напоминаем: {when} в {time} вебинар\n\n"
    "<b>{title}</b>\n\n"
    "Ждём вас 🙂"
)

# Месяцы для {date}: «30 сентября»
MONTHS_GENITIVE = ("января", "февраля", "марта", "апреля", "мая", "июня",
                   "июля", "августа", "сентября", "октября", "ноября",
                   "декабря")

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

# Первое слово после /broadcast похоже на дату, но бот его не понял.
# Тогда ничего не отправляем: иначе ссылка для одного вебинара ушла бы всем.
TEXT_BROADCAST_BAD_DATE = (
    "Первое слово похоже на дату, но я его не понял: «{token}». "
    "Ничего не отправлено.\n"
    "Дату пишите так: <b>/broadcast 30.09.2026 11:00 текст</b>. "
    "Если это не дата — начните текст с другого слова."
)

# ---------------------------------------------------------------------------
# Список записавшихся (/who)
# ---------------------------------------------------------------------------

TEXT_WHO_EMPTY = "Пока никто не записан."

# Ответ на /check, когда рассылать нечего
TEXT_CHECK_NOTHING = (
    "Проверила: напоминаний на сегодня нет (или они уже отправлены)."
)

TEXT_CHECK_CLEANED = (
    "\nЗаодно прибрала список: {count} — уже перезаписались на конкретный "
    "вебинар, из старой записи «по дате» их убрала."
)

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

# Внизу каждой страницы /who. {first}–{last} — какие по счёту карточки
# показаны, {total} — сколько всего людей, {next} — что написать после /who,
# чтобы получить следующую страницу. Сама она не придёт — её надо попросить.
# Телеграм не даёт боту слать в группу больше ~20 сообщений в минуту, а
# страница — это 17, поэтому просить следующую — не раньше чем через минуту.
TEXT_WHO_PAGE_MORE = (
    "Показаны {first}–{last} из {total}.\n"
    "Чтобы увидеть следующие, отправьте <b>/who {next}</b> — но не раньше "
    "чем через минуту: чаще Телеграм не даёт боту писать в группу."
)

# Если следующую страницу попросили слишком рано. {seconds} — сколько ждать.
TEXT_WHO_WAIT = (
    "Подождите ещё {seconds} сек. и отправьте команду снова: Телеграм не даёт "
    "боту слать в группу больше ~20 сообщений в минуту, а страница — это 17."
)

TEXT_WHO_PAGE_LAST = "Показаны {first}–{last} из {total} — это все."

TEXT_WHO_NO_PAGE = "Страницы {page} нет — всего страниц: {pages}."

# /who с непонятными словами после команды. {text} — что не удалось разобрать.
TEXT_WHO_BAD_ARGS = (
    "Не понял «{text}».\n"
    "Можно так: <b>/who</b> — все, <b>/who 30.09.2026</b> — на эту дату, "
    "<b>/who 30.09.2026 11:00</b> — на этот вебинар. "
    "Число в конце — номер страницы: <b>/who 30.09.2026 11:00 2</b>"
)

TEXT_WHO_NO_WEBINAR = "Вебинара {date} в списке нет."

TEXT_WHO_UNKNOWN_NAME = "без имени"

WHO_MAX = 15   # карточек на странице /who: больше за раз Телеграм не даст отправить подряд

# ---------------------------------------------------------------------------
# Инструкция для участников рабочей группы (/help внутри группы)
# ---------------------------------------------------------------------------

TEXT_ADMIN_HELP = (
    "🛠 <b>Команды в этой группе</b>\n\n"

    "<b>/stats</b> — <b>сколько</b>: человек на каждый вебинар, "
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
    "/who 30.09.2026 19:00. Карточки идут по 15; число в конце — номер "
    "страницы: /who 30.09.2026 19:00 2\n\n"

    "<b>/cleanup</b> — убрать из списка записи на прошедшие вебинары. Сначала "
    "покажет, что именно удалит, и спросит подтверждение. Эти люди перестанут "
    "получать /broadcast.\n"
    "<i>Когда нужно:</i> почти никогда — места в списке хватает всегда, это "
    "только для порядка.\n\n"

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
    "Дата строго в формате ДД.ММ.ГГГГ, время — по Москве. Напоминания "
    "уходят сами — записывать их отдельно не нужно."
)

# Строчка в /stats: где лежит сам список
TEXT_STATS_FILE = (
    "Список — в файле registry.xlsx: это последний файл от бота в этой "
    "группе, его можно скачать и открыть в Excel."
)

TEXT_STATS_SCHEDULE = (
    "<b>Когда уходят напоминания</b>\n"
    "{days}{soon}\n"
    "{wake}"
)

TEXT_STATS_SCHEDULE_DAYS = "{list} — в {at} ({tz})"
TEXT_STATS_SCHEDULE_NO_DAYS = "напоминания за несколько дней выключены"
TEXT_STATS_SCHEDULE_SOON = (
    "\nи ещё раз примерно за {minutes} минут до начала "
    "(время вебинаров — {wtz})"
)
TEXT_STATS_SCHEDULE_WAKE = (
    "Чтобы всё это ушло, сервис должен не спать в: {times} ({tz}). "
    "Это и есть список будильников, которые его будят."
)
TEXT_STATS_SCHEDULE_NO_WAKE = "Напоминания выключены — будильники не нужны."

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
    "Всего {removed}.\n\n"
    "⚠️ Эти люди больше не получат рассылку через /broadcast. "
    "Кто и на что записывался, останется видно в сообщениях группы выше."
)

TEXT_CLEANUP_BTN_YES = "Удалить"
TEXT_CLEANUP_BTN_NO = "Отмена"

TEXT_CLEANUP_DONE = "🧹 Готово, убрано {removed}."

TEXT_CLEANUP_CANCELLED = "Отменено, ничего не удалено."

TEXT_CLEANUP_FAILED = (
    "Не получилось сохранить изменения — список остался как был."
)

TEXT_GROUP_REMINDER_SENT = (
    "🔔 Отправлено напоминание про вебинар {date}.\n"
    "Доставлено: {sent}, не доставлено: {failed}."
)

# В тексте напоминания ошибка в фигурных скобках. {error} — что бот не понял.
TEXT_GROUP_REMINDER_TEMPLATE_BROKEN = (
    "⚠️ В тексте напоминания ошибка в фигурных скобках: {error}\n"
    "Напоминание всё равно ушло, но в упрощённом виде. Поправьте "
    "TEXT_REMINDER или TEXT_REMINDER_OTHER_TIMES в bot.py: в скобках можно "
    "писать только {{when}}, {{time}}, {{title}}, {{speaker}} и "
    "{{other_times}}, а в TEXT_REMINDER_OTHER_TIMES — {{date}} и {{times}}."
)

# Список записавшихся хранится в файле registry.txt в этой группе.
# Подпись к этому файлу:
TEXT_REGISTRY_FILE_CAPTION = (
    "📋 Список записавшихся — служебный файл бота. Его можно скачать и открыть "
    "в Excel, но не удаляйте его."
)

# Строка в закреплённом сообщении, когда сам список в него уже не помещается
TEXT_REGISTRY_IN_FILE = (
    "Сам список — в файле registry.xlsx, последнем от бота. Бот читает его сам."
)

# Заголовки столбцов в registry.xlsx — по строке на каждую запись на вебинар.
# Названия можно менять, порядок — нет: бот читает столбцы по порядку.
TEXT_FILE_HEADER = ("id", "вебинар", "имя", "username", "записан(а), МСК")
TEXT_FILE_SHEET = "Записи"      # название листа в Excel

# Бот не может прочитать файл со списком. {error} — что пошло не так.
TEXT_GROUP_REGISTRY_UNREADABLE = (
    "⚠️ Не могу прочитать файл со списком записавшихся: {error}\n"
    "Пока это так, новые регистрации НЕ сохраняются. Напишите разработчику."
)

# Телеграм не понял HTML-разметку в тексте напоминания, и оно ушло тем же
# текстом без форматирования. {error} — что именно ему не понравилось.
TEXT_GROUP_REMINDER_MARKUP_BROKEN = (
    "⚠️ Телеграм не принял разметку в тексте напоминания: {error}\n"
    "Напоминание всё равно ушло — тем же текстом, только без жирного шрифта. "
    "Проверьте теги в TEXT_REMINDER и TEXT_REMINDER_OTHER_TIMES в bot.py: "
    "каждый &lt;b&gt; должен закрываться &lt;/b&gt;, а знаки &lt;, &gt; и "
    "&amp; вне тегов писать нельзя."
)

# Напоминание не дошло ни до кого. Отправленным оно НЕ считается.
TEXT_GROUP_REMINDER_NOT_SENT = (
    "⚠️ Напоминание про вебинар {date} не дошло ни до кого "
    "(ошибок: {failed}). Отправленным оно не считается — повторить можно "
    "командой /check."
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


def webinar_start(w):
    """Начало вебинара как точный момент. Время в WEBINARS — по WEBINAR_TIMEZONE."""
    webinar_date = parse_date(w["date"])
    start_time = parse_time(w.get("time"))
    if webinar_date is None or start_time is None:
        return None
    return WEBINAR_TIMEZONE.localize(
        datetime.datetime.combine(webinar_date, start_time))


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
# записался, хранится в самой рабочей группе: в файле registry.xlsx, на который
# указывает закреплённое сообщение (строка FILE:). В файле — по строке на
# каждую запись: id, вебинар, имя, username, когда записался. Отметки об
# отправленных напоминаниях (SENT:) — в самом закреплённом сообщении.
# ============================================================================

REGISTRY_HEADER = "📋 СПИСОК ЗАРЕГИСТРИРОВАННЫХ (не удалять, не редактировать)"

# дата вебинара -> множество id тех, кто на него записался
registrations = {}
reminded_keys = set()    # по каким вебинарам напоминание уже уходило
registry_message_id = None

# Удалось ли прочитать список при старте. Пока False — записывать НЕЛЬЗЯ:
# пустая память затрёт закреплённое сообщение со всеми регистрациями.
registry_loaded = False

# Предела у списка больше нет: файл бот может скачать до 20 МБ — это десятки
# тысяч записей. (Пока список жил в самом закреплённом сообщении, пределом
# были его 4096 символов, и бот предупреждал на 80% и 95%.)
warned_levels = set()        # старые отметки WARN: — читаем и храним как есть

REGISTRY_FILENAME = "registry.xlsx"
# 25.09 вечером список был в CSV — его бот ещё умеет прочитать и переписать
CSV_DELIMITER = ";"
# В каком виде был файл при последнем чтении: "xlsx", "csv", "text" или None.
# Если не в xlsx — бот перепишет его сразу после запуска.
registry_file_format = None
# Имя и @username каждого, кто записался: id -> (имя, username).
people = {}
# Когда человек записался на вебинар: (ключ вебинара, id) -> '2026-09-25 17:24'
# по Москве. У записавшихся до 25.09 этого нет — тогда время не хранилось.
signed_at = {}
BACKFILL_MAX = 300           # сколько имён узнавать у Телеграма за один запуск
# Пока список помещается в закреплённое сообщение, он лежит и там — рядом с
# указателем на файл. Это страховка на случай отката на старую версию бота:
# та умеет читать только само закреплённое сообщение. Запас — на эмодзи и
# строки FILE:/PREV:.
PIN_LIMIT = 4096 - 200
# Сообщение с файлом текущего списка и предыдущего: (message_id, file_id).
file_pointer = None
prev_pointer = None
# Удалять ли в группе файлы старше предыдущего. Проверено 25.09.2026 на
# настоящем Телеграме: file_id остаётся рабочим и после удаления сообщения
# с файлом (скачивание через 2 и 12 секунд после удаления прошло). Значит,
# удаление старых файлов не может испортить текущий, и в группе их не больше
# двух. False — оставлять все (безопасно, но по файлу на каждое сохранение).
DELETE_OLD_REGISTRY_FILES = True
# Сохранения идут по одному: напоминания рассылаются в своём потоке, и два
# сохранения сразу могли бы записать в файл и в сообщение разное.
_save_lock = threading.Lock()
_read_failures = 0           # сколько раз подряд не удалось прочитать файл

_POINTER_LINE = re.compile(r"^(FILE|PREV):(\d+):(\S+)$")

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


def prune_resolved() -> int:
    """Убирает из старой записи «по дате» тех, кто уже перезаписался на
    конкретный вебинар в этот же день.

    Иначе человек навсегда остаётся сразу в двух строках: и в старой без
    времени, и в новой с временем. Старая строка так постепенно тает и в
    конце концов исчезает совсем.

    Перезапись на вебинар в ДРУГОЙ день не считается: на этот день человек
    по-прежнему непонятно куда записан.
    """
    removed = 0
    for key in list(registrations):
        if not _DATE_ONLY.match(key):
            continue
        same_day = webinars_on(key)
        if len(same_day) <= 1:
            continue
        covered = set()
        for w in same_day:
            covered |= registrations.get(webinar_key(w), set())
        resolved = registrations[key] & covered
        if resolved:
            registrations[key] -= resolved
            removed += len(resolved)
            logger.info("Убрано из непривязанных %s: %s чел. (перезаписались)",
                        key, len(resolved))
        if not registrations[key]:
            del registrations[key]
    return removed


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


def registry_text(snap=None) -> str:
    """Список в старом текстовом виде — он же копия в закреплённом сообщении.

    snap — снимок registrations: сохранение строит из одного снимка и файл,
    и сообщение, чтобы «Всего» в них не разошлось.
    """
    regs = registrations if snap is None else snap
    everyone = set().union(*regs.values()) if regs else set()
    lines = [REGISTRY_HEADER, f"Всего: {len(everyone)}"]
    for key in sorted(regs,
                      key=lambda k: (key_date(k) or datetime.date.max, k)):
        ids = regs[key]
        if ids:
            lines.append(f"{key}:" + ",".join(str(i) for i in sorted(ids)))
    lines.append("SENT:" + ",".join(sorted(reminded_keys)))
    if warned_levels:
        lines.append("WARN:" + ",".join(str(l) for l in sorted(warned_levels)))
    return "\n".join(lines)


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


def parse_registry(text: str):
    """Разбирает текст списка — из закреплённого сообщения или из файла.

    Возвращает (записи, отметки SENT, пороги WARN, указатели, «Всего»).
    Память не трогает: это делает load_registry, дополняя, а не заменяя.
    """
    regs, sent, warn, pointers, total = {}, set(), set(), {}, None
    # Разбираем по признаку строки, а не по её номеру: так порядок строк
    # и появление новых вебинаров ничего не ломают.
    for line in text.split("\n")[1:]:
        line = line.strip()
        if line.startswith("Всего:"):
            digits = line[len("Всего:"):].strip()
            total = int(digits) if digits.isdigit() else None
            continue

        pointer = _POINTER_LINE.match(line)
        if pointer:
            pointers[pointer.group(1)] = (int(pointer.group(2)), pointer.group(3))
            continue

        if line.startswith("SENT:"):
            for d in line[5:].split(","):
                if d.strip():
                    sent.add(migrate_sent(d.strip()))
            continue

        if line.startswith("WARN:"):
            for lvl in line[5:].split(","):
                if lvl.strip().isdigit():
                    warn.add(int(lvl.strip()))
            continue

        match = _KEY_LINE.match(line)
        if match:
            key, ids_part = match.group(1), match.group(2)
            ids = {int(p) for p in (x.strip() for x in ids_part.split(","))
                   if p.isdigit()}
            if ids:
                regs.setdefault(migrate_key(key), set()).update(ids)
            continue

        # Старый формат: голая строка из id без даты. Регистрации тогда были
        # общими, а не по вебинарам, поэтому переносить их некуда.
        if line and line.replace(",", "").isdigit():
            logger.warning(
                "В списке найдена строка в старом формате (без дат) — она "
                "пропущена: %s", line[:80],
            )
    return regs, sent, warn, pointers, total


def download_registry(bot, file_id: str) -> bytes:
    """Скачивает файл списка из группы."""
    return bytes(bot.get_file(file_id).download_as_bytearray())


def snapshot():
    """Копия registrations, которую другой поток уже не поменяет."""
    return {k: v.copy() for k, v in registrations.copy().items()}


def now_msk() -> str:
    """Время записи — по Москве, как и время вебинаров: '2026-09-25 17:24'."""
    return datetime.datetime.now(WEBINAR_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def remember_person(user_id: int, name: str, username: str) -> None:
    people[user_id] = ((name or "").strip(), username or "")


# В CSV (25.09 вечером) имя, похожее на формулу Excel, хранилось с апострофом
# впереди. При чтении такого файла апостроф снимаем.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_unsafe(value: str) -> str:
    if value.startswith("'") and value[1:].startswith(_FORMULA_START):
        return value[1:]
    return value


def registry_xlsx(snap) -> bytes:
    """registry.xlsx: строка на каждую запись на вебинар."""
    wb = Workbook()
    ws = wb.active
    ws.title = TEXT_FILE_SHEET
    ws.append(list(TEXT_FILE_HEADER))
    for cell in ws[1]:
        cell.font = Font(bold=True)
    row = 1
    for key in sorted(snap, key=lambda k: (key_date(k) or datetime.date.max, k)):
        for uid in sorted(snap[key], key=lambda u: (signed_at.get((key, u), ""), u)):
            row += 1
            name, username = people.get(uid, ("", ""))
            ws.cell(row=row, column=1, value=uid)
            # Текст остаётся текстом: имя вроде «=HYPERLINK(...)» openpyxl
            # иначе записал бы формулой, а Excel бы её выполнил.
            for col, value in ((2, key), (3, name), (4, username)):
                cell = ws.cell(row=row, column=col, value=value)
                cell.data_type = "s"
            when = signed_at.get((key, uid))
            if when:
                cell = ws.cell(row=row, column=5, value=datetime.datetime.strptime(
                    when, "%Y-%m-%d %H:%M"))
                cell.number_format = "yyyy-mm-dd hh:mm"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:E{row}"
    for letter, width in zip("ABCDE", (13, 17, 32, 20, 18)):
        ws.column_dimensions[letter].width = width
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def parse_registry_xlsx(data: bytes):
    """Разбирает registry.xlsx. Возвращает (записи, люди, время записи).

    Непонятная строка — ошибка, а не пропуск: это наш собственный файл, и
    испорченный файл нельзя принять за прочитанный.
    """
    regs, ppl, times = {}, {}, {}
    wb = load_workbook(io.BytesIO(data), read_only=True)
    try:
        rows = wb.worksheets[0].iter_rows(values_only=True)
        next(rows, None)                  # заголовки столбцов
        for row in rows:
            if not row or all(v in (None, "") for v in row):
                continue
            uid, key = row[0], str(row[1] or "").strip()
            if isinstance(uid, float) and uid.is_integer():
                uid = int(uid)
            if not isinstance(uid, int) or not _KEY_LINE.match(key + ":"):
                raise ValueError(f"непонятная строка в файле: {str(row[:2])[:60]}")
            key = migrate_key(key)
            regs.setdefault(key, set()).add(uid)
            name = str(row[2] or "") if len(row) > 2 else ""
            username = str(row[3] or "") if len(row) > 3 else ""
            if name or username:
                ppl[uid] = (name, username)
            when = row[4] if len(row) > 4 else None
            if isinstance(when, datetime.datetime):
                times[(key, uid)] = when.strftime("%Y-%m-%d %H:%M")
            elif when:
                times[(key, uid)] = str(when).strip()
    finally:
        wb.close()
    return regs, ppl, times


def parse_registry_csv(data: str):
    """Разбирает registry.csv. Возвращает (записи, люди, время записи).

    Непонятная строка — ошибка, а не пропуск: это наш собственный файл, и
    испорченный файл нельзя принять за прочитанный.
    """
    regs, ppl, times = {}, {}, {}
    rows = csv.reader(io.StringIO(data), delimiter=CSV_DELIMITER)
    next(rows, None)                      # заголовки столбцов
    for row in rows:
        if not row:
            continue
        key = row[1].strip() if len(row) > 1 else ""
        if not row[0].strip().isdigit() or not _KEY_LINE.match(key + ":"):
            raise ValueError(f"непонятная строка в файле: {';'.join(row[:2])[:60]}")
        uid, key = int(row[0]), migrate_key(key)
        regs.setdefault(key, set()).add(uid)
        name = csv_unsafe(row[2]) if len(row) > 2 else ""
        username = row[3].strip() if len(row) > 3 else ""
        if name or username:
            ppl[uid] = (name, username)
        if len(row) > 4 and row[4].strip():
            times[(key, uid)] = row[4].strip()
    return regs, ppl, times


def prune_past_sent(today: datetime.date = None) -> int:
    """Отметки о напоминаниях про прошедшие вебинары больше не нужны.

    Теперь они живут в закреплённом сообщении, а оно не резиновое. Напоминание
    про прошедший вебинар уйти уже не может, так что и отметка ни к чему.
    """
    if today is None:
        today = today_local()
    stale = {t for t in reminded_keys
             if (key_date(t.split("@", 1)[0]) or today) < today}
    reminded_keys.difference_update(stale)
    return len(stale)


def backfill_names(bot) -> None:
    """Узнаёт имена тех, кто записался, пока бот имён не хранил (до 25.09).

    Идёт в своём потоке после запуска, по одному запросу в 0,1 с, и в конце
    один раз сохраняет список — уже с именами.
    """
    everyone = set().union(*snapshot().values()) if registrations else set()
    missing = sorted(u for u in everyone if u not in people)
    found = 0
    for uid in missing[:BACKFILL_MAX]:
        try:
            chat = bot.get_chat(uid)
            remember_person(uid, chat.full_name, chat.username)
            found += 1
        except TelegramError as e:
            logger.info("Не удалось узнать имя %s: %s", uid, e)
        time.sleep(0.1)
    if missing:
        logger.info("Имена: узнали %s из %s", found, len(missing))
    # Сохраняем, если узнали новые имена или файл ещё в старом виде (CSV,
    # текст) — тогда он сразу станет registry.xlsx, не дожидаясь регистрации.
    if found or (file_pointer and registry_file_format != "xlsx"):
        save_registry(bot)


def load_registry(bot) -> bool:
    """Читает список записавшихся: закреплённое сообщение и файл, на который оно указывает.

    True — прочитали (в том числе «списка ещё нет, он пустой»).
    False — не смогли; писать в этом состоянии нельзя.
    Прочитанное ДОПОЛНЯЕТ память, а не заменяет её: так регистрация, принятая,
    пока группа была недоступна, переживает повторное чтение перед записью.
    """
    global registry_message_id, registry_loaded, file_pointer, prev_pointer
    global _read_failures, registry_file_format

    if not ADMIN_CHAT_ID:
        logger.error(
            "ADMIN_CHAT_ID не задан! Регистрации НЕ сохраняются, уведомления "
            "в группу НЕ приходят, служебные команды отключены."
        )
        return False

    try:
        chat = bot.get_chat(ADMIN_CHAT_ID)
    except TelegramError as e:
        registry_loaded = False
        logger.error("Не удалось прочитать список подписчиков: %s", e)
        return False

    pinned = chat.pinned_message
    if not (pinned and pinned.text and pinned.text.startswith(REGISTRY_HEADER)):
        logger.info("Закреплённого списка нет — будет создан при первой регистрации")
        registry_loaded = True
        return True

    registry_message_id = pinned.message_id
    regs, sent, warn, pointers, total = parse_registry(pinned.text)

    if "FILE" in pointers:
        # Список — в файле, и верим именно файлу. Копия в самом сообщении
        # (если она ещё помещается) — только для отката на старую версию.
        try:
            data = download_registry(bot, pointers["FILE"][1])
            if data[:2] == b"PK":                 # xlsx — это zip-архив
                regs, ppl, times = parse_registry_xlsx(data)
                fmt = "xlsx"
            elif data.decode("utf-8-sig").startswith(REGISTRY_HEADER):
                # Файл в старом текстовом виде (так было 25.09 до вечера):
                # отметки SENT тогда жили в нём, а не в сообщении.
                regs, fsent, fwarn, _, _ = parse_registry(data.decode("utf-8"))
                sent |= fsent
                warn |= fwarn
                ppl, times = {}, {}
                fmt = "text"
            else:                                 # CSV — 25.09 вечером
                regs, ppl, times = parse_registry_csv(data.decode("utf-8-sig"))
                fmt = "csv"
            count = len(set().union(*regs.values())) if regs else 0
            # «Всего» записано в сообщение тем же сохранением, что и файл.
            # Файл, оборвавшийся на границе строки, разбирается без ошибок,
            # но людей в нём меньше — такой файл прочитанным не считаем.
            if total is not None and count != total:
                raise ValueError(f"в файле {count} чел., а в закреплённом "
                                 f"сообщении — {total}")
        except Exception as e:     # сеть, битый файл, неизвестный формат
            if not isinstance(e, (TelegramError, ValueError, UnicodeDecodeError,
                                  csv.Error, zipfile.BadZipFile)):
                logger.exception("Неожиданная ошибка при чтении файла списка")
            registry_loaded = False
            _read_failures += 1
            logger.error("Не удалось прочитать файл списка: %s", e)
            # Один сбой на холодном старте обычно проходит сам при следующей
            # записи — в группу пишем, только если не вышло и во второй раз.
            if _read_failures == 2:
                notify_group(bot, TEXT_GROUP_REGISTRY_UNREADABLE.format(
                    error=html.escape(str(e))))
            return False
        file_pointer = pointers["FILE"]
        prev_pointer = pointers.get("PREV")
        registry_file_format = fmt

    for key, ids in regs.items():
        registrations.setdefault(key, set()).update(ids)
    if "FILE" in pointers:
        # то, что память уже знает (свежая регистрация), не затираем
        for uid, info in ppl.items():
            people.setdefault(uid, info)
        for pair, when in times.items():
            signed_at.setdefault(pair, when)
    reminded_keys.update(sent)
    warned_levels.update(warn)
    _read_failures = 0

    logger.info(
        "Загружено (%s): вебинаров с записями %s, человек всего %s, "
        "отправленных напоминаний %s",
        "из файла" if file_pointer else "из закреплённого сообщения",
        len(registrations), len(all_subscribers()), len(reminded_keys),
    )
    registry_loaded = True
    return True


def pin_text(text: str) -> str:
    """Закреплённое сообщение: указатель на файл и, пока влезает, сам список.

    text — тот же снимок списка, что ушёл в файл, чтобы «Всего» в сообщении
    и файл никогда не расходились.
    """
    pointer = []
    if file_pointer:
        pointer.append(f"FILE:{file_pointer[0]}:{file_pointer[1]}")
    if prev_pointer:
        pointer.append(f"PREV:{prev_pointer[0]}:{prev_pointer[1]}")
    full = "\n".join([text] + pointer)
    if len(full) <= PIN_LIMIT:
        return full
    lines = text.split("\n")
    total_line = next(l for l in lines if l.startswith("Всего:"))
    # Отметки о напоминаниях в файле больше не хранятся — только здесь
    marks = [l for l in lines if l.startswith(("SENT:", "WARN:"))]
    return "\n".join([REGISTRY_HEADER, total_line, TEXT_REGISTRY_IN_FILE]
                     + marks + pointer)


def with_retry(call):
    """Вызов к Телеграму с одним повтором, если он просит подождать недолго.

    В группу нельзя слать больше ~20 сообщений в минуту, а каждое сохранение
    теперь — это ещё и файл. Короткую паузу переждём, длинную — нет.
    """
    try:
        return call()
    except RetryAfter as e:
        if e.retry_after > 15:
            raise
        logger.warning("Телеграм просит подождать %s с — жду и повторяю",
                       e.retry_after)
        time.sleep(e.retry_after + 1)
        return call()


def delete_quietly(bot, message_id: int) -> None:
    """Удаляет старый файл списка; не вышло — не беда, он просто останется."""
    try:
        bot.delete_message(chat_id=ADMIN_CHAT_ID, message_id=message_id)
    except TelegramError as e:
        logger.info("Не удалось удалить старый файл списка %s: %s",
                    message_id, e)


def save_registry(bot) -> bool:
    """Сохраняет список: новый файл в группе, потом указатель на него в
    закреплённом сообщении. True — сохранено (или сохранять некуда)."""
    with _save_lock:
        return _save_registry(bot)


def _save_registry(bot) -> bool:
    global registry_message_id, file_pointer, prev_pointer, registry_file_format

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

    # Кто перезаписался — того в старой строке «по дате» держать незачем
    prune_resolved()

    prune_past_sent()

    # Один снимок на всё сохранение: из него и файл, и сообщение — чтобы
    # регистрация, пришедшая посередине, не развела их «Всего».
    snap = snapshot()
    text = registry_text(snap)
    data = registry_xlsx(snap)

    # 1. Сначала новый файл. Не загрузился — ничего не поменялось.
    try:
        doc = with_retry(lambda: bot.send_document(
            chat_id=ADMIN_CHAT_ID,
            document=io.BytesIO(data),
            filename=REGISTRY_FILENAME,
            caption=TEXT_REGISTRY_FILE_CAPTION,
            disable_notification=True,
        ))
    except TelegramError as e:
        logger.error("Не удалось загрузить файл списка: %s", e)
        return False

    # 2. Потом указатель на него. Не обновился — сообщение по-прежнему
    #    указывает на старый файл, а новый просто лежит лишним. Удалять его
    #    нельзя: при обрыве связи правка могла и пройти.
    old_file, old_prev = file_pointer, prev_pointer
    file_pointer = (doc.message_id, doc.document.file_id)
    prev_pointer = old_file
    try:
        if registry_message_id:
            with_retry(lambda: bot.edit_message_text(
                chat_id=ADMIN_CHAT_ID,
                message_id=registry_message_id,
                text=pin_text(text),
            ))
        else:
            msg = with_retry(lambda: bot.send_message(
                chat_id=ADMIN_CHAT_ID, text=pin_text(text)))
            registry_message_id = msg.message_id
            bot.pin_chat_message(
                chat_id=ADMIN_CHAT_ID,
                message_id=msg.message_id,
                disable_notification=True,
            )
    except TelegramError as e:
        file_pointer, prev_pointer = old_file, old_prev
        logger.error("Не удалось сохранить список подписчиков: %s", e)
        return False

    # 3. Файл старше предыдущего больше нигде не упомянут
    if old_prev and DELETE_OLD_REGISTRY_FILES:
        delete_quietly(bot, old_prev[0])
    registry_file_format = "xlsx"
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


# Тег HTML: <b>, </b>, <a href="...">. «<3» или «< 5» тегом не считаются.
_HTML_TAG = re.compile(r"</?[a-zA-Z][^<>]*>")
_HTML_LINK = re.compile(
    r"""<a\s[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""", re.S | re.I)


def is_markup_error(error) -> bool:
    """Телеграм не понял HTML-разметку самого текста (у всех она одна)."""
    return (isinstance(error, BadRequest)
            and "can't parse entities" in str(error).lower())


def plain_text(text: str) -> str:
    """Тот же текст без разметки: '<b>x</b> &amp; <a href="u">y</a>' -> 'x & y (u)'."""
    text = _HTML_LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
    return html.unescape(_HTML_TAG.sub("", text))


def broadcast(bot, text: str, recipients, markup_fallback: bool = False):
    """Рассылает текст указанным людям.

    Возвращает (доставлено, ошибок, ошибка разметки или None).
    markup_fallback — для напоминаний, которые уходят без человека рядом: если
    Телеграм не понял HTML-разметку, тот же текст уходит всем без неё. В
    /broadcast этого нет: там ошибку сразу видит тот, кто отправлял.
    """
    sent, failed, gone = 0, 0, []
    parse_mode, markup_error = 'HTML', None
    for user_id in sorted(recipients):
        try:
            try:
                bot.send_message(chat_id=user_id, text=text,
                                 parse_mode=parse_mode)
            except BadRequest as e:
                if not (markup_fallback and parse_mode and is_markup_error(e)):
                    raise
                # Разметка сломана в самом тексте — значит, у всех. Этому
                # человеку ещё раз, остальным сразу без разметки.
                logger.error("Телеграм не понял разметку: %s — отправляю "
                             "без форматирования", e)
                markup_error, text, parse_mode = e, plain_text(text), None
                bot.send_message(chat_id=user_id, text=text, parse_mode=None)
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
    return sent, failed, markup_error


# ============================================================================
# АВТОМАТИЧЕСКИЕ НАПОМИНАНИЯ
# ============================================================================


def date_words(date_str: str) -> str:
    """'30.09.2026' -> '30 сентября' — для {date} в напоминании."""
    d = parse_date(date_str)
    if d is None:
        return date_str
    return f"{d.day} {MONTHS_GENITIVE[d.month - 1]}"


def times_phrase(times) -> str:
    """['11:00', '19:00'] -> '11:00 или 19:00'; три и больше — через запятую."""
    times = list(times)
    if len(times) <= 1:
        return "".join(times)
    return ", ".join(times[:-1]) + " или " + times[-1]


def speaker_of(w) -> str:
    """Имя спикера — часть названия до первого двоеточия."""
    name, colon, _ = clean_title(w).partition(": ")
    return name.strip() if colon and name.strip() else TEXT_SPEAKER_FALLBACK


_TEMPLATE_ERRORS = (KeyError, IndexError, ValueError, AttributeError)


def reminder_text(w, when: str, other_times: bool):
    """Текст напоминания про вебинар w. Возвращает (текст, ошибка или None).

    Шаблоны правит владелец, поэтому ошибка в фигурных скобках не должна
    останавливать рассылку: без абзаца про другое время или по запасному
    тексту — но напоминание уходит, а об ошибке узнаёт группа.
    """
    error = None
    extra = ""
    same_day = sorted(webinars_on(w["date"]),
                      key=lambda x: parse_time(x.get("time")) or datetime.time.max)
    times = list(dict.fromkeys(x["time"] for x in same_day))
    if other_times and len(times) > 1:
        try:
            extra = TEXT_REMINDER_OTHER_TIMES.format(
                date=date_words(w["date"]), times=times_phrase(times))
        except _TEMPLATE_ERRORS as e:
            logger.error("Ошибка в TEXT_REMINDER_OTHER_TIMES: %r", e)
            error = e
    try:
        return TEXT_REMINDER.format(
            when=when, time=w["time"], title=clean_title(w),
            speaker=speaker_of(w), other_times=extra), error
    except _TEMPLATE_ERRORS as e:
        logger.error("Ошибка в TEXT_REMINDER: %r — отправляю запасной текст", e)
        return TEXT_REMINDER_FALLBACK.format(
            when=when, time=w["time"], title=clean_title(w)), e


def warn_markup(bot, error) -> None:
    """Сообщает в группу, что Телеграм не понял разметку напоминания."""
    notify_group(bot, TEXT_GROUP_REMINDER_MARKUP_BROKEN.format(
        error=html.escape(str(error))))


# О каком напоминании уже написали «не дошло ни до кого». Напоминание «перед
# началом» проверяется каждые 5 минут — без этого группа получала бы по
# сообщению на каждую неудачную попытку.
_reported_not_sent = set()


def report_not_sent(bot, tag: str, label: str, failed: int) -> None:
    """Одно сообщение в группу на напоминание, которое не дошло ни до кого."""
    if tag in _reported_not_sent:
        return
    _reported_not_sent.add(tag)
    notify_group(bot, TEXT_GROUP_REMINDER_NOT_SENT.format(
        date=label, failed=failed))


def warn_template(bot, error) -> None:
    """Сообщает в группу, что в шаблоне напоминания ошибка."""
    notify_group(bot, TEXT_GROUP_REMINDER_TEMPLATE_BROKEN.format(
        error=html.escape(str(error))))


def send_reminders(bot, today: datetime.date = None) -> int:
    """Проверяет, есть ли вебинар завтра, и если да — рассылает напоминание."""
    if today is None:
        today = today_local()

    total = 0
    warned = set()   # о какой поломке шаблона уже написали в группу
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

        text, template_error = reminder_text(
            w, when_phrase(days_left), other_times=True)
        if template_error and "braces" not in warned:
            warn_template(bot, template_error)
            warned.add("braces")
        sent, failed, markup_error = broadcast(bot, text, recipients,
                                               markup_fallback=True)
        if markup_error and "markup" not in warned:
            warn_markup(bot, markup_error)
            warned.add("markup")
        total += sent

        logger.info("Напоминание про %s за %s дн.: доставлено %s, ошибок %s",
                    key, days_left, sent, failed)
        label = f"{w['date']} в {w['time']} ({when_phrase(days_left)})"
        if not sent:
            # Не дошло ни до кого — не отмечаем, чтобы /check мог повторить.
            report_not_sent(bot, sent_tag(key, tag), label, failed)
            continue
        reminded_keys.add(sent_tag(key, tag))
        save_registry(bot)
        notify_group(bot, TEXT_GROUP_REMINDER_SENT.format(
            date=label, sent=sent, failed=failed))
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
    warned = set()   # о какой поломке шаблона уже написали в группу
    for w in WEBINARS:
        # Начало — по WEBINAR_TIMEZONE, now — по TIMEZONE: разность двух
        # точных моментов от поясов не зависит.
        start = webinar_start(w)
        if start is None:
            continue
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

        text, template_error = reminder_text(
            w, TEXT_WHEN_SOON, other_times=False)
        if template_error and "braces" not in warned:
            warn_template(bot, template_error)
            warned.add("braces")
        sent, failed, markup_error = broadcast(bot, text, recipients,
                                               markup_fallback=True)
        if markup_error and "markup" not in warned:
            warn_markup(bot, markup_error)
            warned.add("markup")
        total += sent

        logger.info("Напоминание перед началом %s: доставлено %s, ошибок %s",
                    key, sent, failed)
        label = f"{w['date']} в {w['time']} (перед началом)"
        if not sent:
            # Не дошло ни до кого — не отмечаем: через 5 минут бот попробует
            # снова, пока не начался вебинар.
            report_not_sent(bot, sent_tag(key, "soon"), label, failed)
            continue
        reminded_keys.add(sent_tag(key, "soon"))
        save_registry(bot)
        notify_group(bot, TEXT_GROUP_REMINDER_SENT.format(
            date=label, sent=sent, failed=failed))
    return total


def daily_job(context: CallbackContext) -> None:
    send_reminders(context.bot)


def soon_job(context: CallbackContext) -> None:
    """Пока сервис не спит, каждые несколько минут проверяем, не начинается ли
    вебинар совсем скоро. Так напоминание уйдёт, даже если сервис проснулся
    не ровно в нужную минуту."""
    if REMINDER_BEFORE_START:
        send_start_reminders(context.bot)


def wakeup_times(today: datetime.date = None):
    """Во сколько по TIMEZONE сервис обязан не спать, чтобы напоминания ушли.

    Это и есть список будильников, которые надо завести: время напоминаний
    «за столько-то дней» плюс по одному перед началом каждого вебинара.
    Вебинары записаны по WEBINAR_TIMEZONE, а разница поясов меняется при
    переходе на зимнее время, поэтому переводим для каждой даты отдельно —
    в списке могут оказаться оба варианта.
    """
    needed = set()
    if REMINDER_DAYS:
        needed.add(f"{REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d}")
    if REMINDER_BEFORE_START:
        for w in upcoming_webinars(today):
            start = webinar_start(w)
            if start is None:
                continue
            # Будильник ставим чуть ПОЗЖЕ начала окна, а не раньше: сервис
            # просыпается примерно на 15 минут, и эти 15 минут должны целиком
            # попасть внутрь окна. Если будить до его открытия, на проверки
            # внутри окна остаётся всего несколько минут.
            moment = start - datetime.timedelta(minutes=MINUTES_BEFORE_START - 5)
            needed.add(moment.astimezone(TIMEZONE).strftime("%H:%M"))
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
    remember_person(user.id, user.full_name, user.username)
    signed_at[(key, user.id)] = now_msk()
    if not save_registry(context.bot):
        registrations[key].discard(user.id)
        signed_at.pop((key, user.id), None)
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
    times = wakeup_times()
    text += "\n\n" + TEXT_STATS_SCHEDULE.format(
        days=(TEXT_STATS_SCHEDULE_DAYS.format(
                  list=", ".join(f"за {d} " + plural_ru(d, "день", "дня", "дней")
                                 for d in sorted(REMINDER_DAYS, reverse=True)),
                  at=f"{REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d}", tz=TIMEZONE_LABEL)
              if REMINDER_DAYS else TEXT_STATS_SCHEDULE_NO_DAYS),
        soon=(TEXT_STATS_SCHEDULE_SOON.format(minutes=MINUTES_BEFORE_START,
                                              wtz=WEBINAR_TIMEZONE_LABEL)
              if REMINDER_BEFORE_START else ""),
        wake=(TEXT_STATS_SCHEDULE_WAKE.format(times=", ".join(times),
                                              tz=TIMEZONE_LABEL)
              if times else TEXT_STATS_SCHEDULE_NO_WAKE),
    )

    text += "\n\n" + TEXT_STATS_FILE

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
            remember_person(user_id, chat.full_name, chat.username)
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
# Так вебинар записан внутри бота: 30.09.2026-1100. Раз человек это где-то
# видел, он так и напишет — понимаем и такую запись.
_KEY_FORM = re.compile(r"^(\d{2}\.\d{2}\.\d{4})-(\d{1,2})(\d{2})$")
# Похоже на дату, но не распозналось: 30.9.2026, 30.09.26, 30.09
_DATE_LIKE = re.compile(r"^\d{1,2}\.\d{1,2}")


def split_target(text: str):
    """'30.09.2026 19:00 текст' -> ('30.09.2026', '19:00', 'текст').

    Понимает и '30.09.2026-1900 текст'. Дата и время необязательны; что не
    распознали — остаётся текстом.
    """
    date_str = time_str = ""
    first, _, tail = text.partition(' ')
    key_form = _KEY_FORM.match(first)
    if key_form:
        date_str, text = key_form.group(1), tail.strip()
        time_str = f"{key_form.group(2)}:{key_form.group(3)}"
    elif _DATE_ONLY.match(first):
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


# Когда /who закончил последнюю страницу. Следующую раньше чем через минуту
# не шлём: Телеграм оборвёт её на середине без всякого объяснения.
_last_who_page = 0.0


def who_command(update: Update, context: CallbackContext) -> None:
    """/who [ДД.ММ.ГГГГ [ЧЧ:ММ]] [страница] — карточки записавшихся по WHO_MAX
    на страницу, по именам можно нажимать."""
    if not is_admin_chat(update):
        return

    w_date, w_time, extra = split_target(
        update.message.text.partition(' ')[2].strip())
    page = 1
    if re.fullmatch(r"[0-9]+", extra):
        page, extra = int(extra), ""
    if extra or page < 1:
        update.message.reply_text(
            TEXT_WHO_BAD_ARGS.format(text=html.escape(extra or str(page))),
            parse_mode='HTML')
        return

    wanted = f"{w_date}-{re.sub(r'[^0-9]', '', w_time)}" if w_time else w_date
    target = f"{w_date} {w_time}" if w_time else w_date   # как пишут после /who
    label = f"{w_date} в {w_time}" if w_time else w_date  # как показываем людям
    if wanted:
        dates = keys_for(wanted)
        if not dates:
            update.message.reply_text(TEXT_WHO_NO_WEBINAR.format(date=label))
            return
        if not any(registrations.get(k) for k in dates):
            update.message.reply_text(
                TEXT_BROADCAST_EMPTY_ONE.format(date=label))
            return
    else:
        dates = sorted(
            (k for k, ids in registrations.items() if ids),
            key=lambda k: (key_date(k) or datetime.date.max, k),
        )
    if not dates:
        update.message.reply_text(TEXT_WHO_EMPTY)
        return

    # Каждый человек — один раз, сколько бы вебинаров у него ни было.
    on_dates = {}
    for date_str in dates:
        for uid in registrations.get(date_str, set()):
            on_dates.setdefault(uid, []).append(date_str)

    # Порядок по id: он не меняется между вызовами, так что страницы не
    # перемешиваются, а имена узнаём только у тех, кто на этой странице.
    people = sorted(on_dates, key=str)
    pages = -(-len(people) // WHO_MAX)
    if page > pages:
        update.message.reply_text(
            TEXT_WHO_NO_PAGE.format(page=page, pages=pages))
        return
    first = (page - 1) * WHO_MAX
    chunk = people[first:first + WHO_MAX]

    global _last_who_page
    wait = 60 - (time.time() - _last_who_page)
    if wait > 0:
        update.message.reply_text(TEXT_WHO_WAIT.format(seconds=int(wait) + 1))
        return
    try:
        _send_who_page(update, context, label, target, page, people, chunk,
                       first, on_dates)
    finally:
        _last_who_page = time.time()


def _send_who_page(update, context, label, target, page, people, chunk,
                   first, on_dates) -> None:
    """Заголовок, карточки и подпись одной страницы /who."""
    try:
        update.message.reply_text(
            TEXT_WHO_HEADER.format(
                scope=TEXT_WHO_SCOPE.format(date=label) if label else ""),
            parse_mode='HTML', disable_web_page_preview=True)
    except TelegramError as e:
        logger.error("Не удалось отправить заголовок /who: %s", e)
        return

    # По карточке на человека: в каждой ровно один, поэтому ответ командой
    # /dm всегда однозначен.
    for uid in chunk:
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
            return
        time.sleep(0.3)   # лимит Телеграма на сообщения в группу

    last = first + len(chunk)
    if last < len(people):
        footer = TEXT_WHO_PAGE_MORE.format(
            first=first + 1, last=last, total=len(people),
            next=f"{target} {page + 1}".strip())
    else:
        footer = TEXT_WHO_PAGE_LAST.format(
            first=first + 1, last=last, total=len(people))
    update.message.reply_text(footer, parse_mode='HTML')


def records_phrase(count: int) -> str:
    """'12 записей' — в нужном падеже."""
    return f"{count} " + plural_ru(count, "запись", "записи", "записей")


def cleanup_command(update: Update, context: CallbackContext) -> None:
    """/cleanup — показать записи на прошедшие вебинары и предложить их убрать."""
    if not is_admin_chat(update):
        return

    past = past_registration_dates()
    if not past:
        update.message.reply_text(TEXT_CLEANUP_NOTHING)
        return

    items = "\n".join(f"• {key_label(date_str)} — {records_phrase(count)}"
                      for date_str, count in past)
    keyboard = [[
        InlineKeyboardButton(TEXT_CLEANUP_BTN_YES, callback_data="cleanup:yes"),
        InlineKeyboardButton(TEXT_CLEANUP_BTN_NO, callback_data="cleanup:no"),
    ]]
    update.message.reply_text(
        TEXT_CLEANUP_PREVIEW.format(
            items=items,
            removed=records_phrase(sum(c for _, c in past)),
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
        removed=records_phrase(removed)))


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

    # «30.9.2026 текст» датой не распознаётся — без этой проверки рассылка
    # ушла бы всем, вместе со ссылкой, которая нужна одному вебинару.
    first_word = rest.partition(' ')[0]
    if not date_str and _DATE_LIKE.match(first_word):
        update.message.reply_text(
            TEXT_BROADCAST_BAD_DATE.format(token=html.escape(first_word)),
            parse_mode='HTML')
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

    sent, failed, _ = broadcast(context.bot, rest, recipients)
    update.message.reply_text(TEXT_BROADCAST_DONE.format(sent=sent, failed=failed))


def check_command(update: Update, context: CallbackContext) -> None:
    """/check — вручную запустить проверку напоминаний (для теста)."""
    if not is_admin_chat(update):
        return
    total = send_reminders(context.bot)
    if REMINDER_BEFORE_START:
        total += send_start_reminders(context.bot)

    # Заодно приборка: send_reminders() сохраняет список только когда реально
    # что-то отправил, поэтому без этого /check не чистит старые записи.
    cleaned = prune_resolved()
    if cleaned:
        save_registry(context.bot)

    if total == 0:
        text = TEXT_CHECK_NOTHING
        if cleaned:
            text += TEXT_CHECK_CLEANED.format(count=records_phrase(cleaned))
        update.message.reply_text(text)


# ============================================================================
# HEALTH-СТРАНИЦА
# Нужна, чтобы будильник разбудил сервис и получил ответ 200, а не ошибку.
# ============================================================================


class HealthHandler(tornado.web.RequestHandler):
    """Страница для будильников, которые будят сервис.

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

    if load_registry(updater.bot) and prune_resolved():
        # На холодном старте подчищаем старые записи тех, кто уже перезаписался.
        # Сервис просыпается часто, поэтому список приходит в порядок сам.
        save_registry(updater.bot)

    if registry_loaded:
        # Имена тех, кто записался до 25.09, — в фоне, чтобы не держать запуск
        threading.Thread(target=backfill_names, args=(updater.bot,),
                         daemon=True).start()

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
