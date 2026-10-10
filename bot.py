import os
import re
import time
import threading
import requests
import vk_api

from concurrent.futures import ThreadPoolExecutor
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

# ==========================================================
# НАСТРОЙКИ
# ==========================================================

# ВСТАВЬ НОВЫЙ токен (старый засветился в чате — отзови его в настройках сообщества)
VK_TOKEN = os.environ.get("VK_TOKEN", "vk1.a._74jNUH2XsupAUEs4x9E77nW5dOFjH4W8_fnoLzl9Aq5weI3PG6rOCiFIOJTq0HxxuTWwrsN13LsSDyiQYLB0r9oXg5ychaqmrySRp76k4_RGnQjo94fsIWw3lEOBhhDSo8p9ugMehGZMl4mGSEFgmjacafPAHLlQxSqz1PO6msQWXb7MWQIQjMbinOTB6duGmJCnykldQBot2b5_0xOJA").strip().strip('"').strip("'")

GROUP_ID = 241841230

SCRIPT_URL = "https://script.google.com/macros/s/AKfycbykzQWDnA468hYkYxXGqpfsdMSdugfekql11RNunw6vfw1Eu39uElFSaw3My8xza0Gw/exec"

# Должен совпадать с SECRET в Google Apps Script
SCRIPT_SECRET = "724422"

# VK ID пользователей, которым доступны админские команды
ALLOWED_IDS = {
    875762552,
    857698829,
    200005206671
}

DEBUG = False

# Таймаут запроса к таблице (сек) и интервал "прогрева" скрипта
SHEET_TIMEOUT = 25
KEEPALIVE_INTERVAL = 240

# Действия, которые только читают (их безопасно повторять при таймауте)
READ_ACTIONS = {"list", "my_points", "all_links", "shop_list", "ping"}

# ==========================================================
# ПОДКЛЮЧЕНИЕ К VK
# ==========================================================

vk_session = vk_api.VkApi(token=VK_TOKEN)
vk = vk_session.get_api()

# ==========================================================
# HTTP-СЕССИЯ (keep-alive, пул соединений)
# ==========================================================

http = requests.Session()
http.headers.update({"Content-Type": "application/json"})

adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20)
http.mount("https://", adapter)

executor = ThreadPoolExecutor(max_workers=8)

# ==========================================================
# ОТПРАВКА СООБЩЕНИЙ
# ==========================================================

def reply(peer_id, text):
    try:
        vk.messages.send(peer_id=peer_id, message=str(text), random_id=0)
    except Exception as e:
        print("Ошибка отправки сообщения:", repr(e))


def notify_admins(text):
    for admin_id in ALLOWED_IDS:
        try:
            vk.messages.send(user_id=admin_id, message=text, random_id=0)
        except Exception as e:
            print(f"Не удалось написать {admin_id}:", repr(e))

# ==========================================================
# ЗАПРОС В GOOGLE APPS SCRIPT
# ==========================================================

def _post_once(payload):
    return http.post(
        SCRIPT_URL,
        json=payload,
        timeout=(5, SHEET_TIMEOUT),
        allow_redirects=True
    )


def call_sheet_ex(payload):
    """Возвращает (текст_ответа, данные_или_None)."""

    payload["secret"] = SCRIPT_SECRET

    is_read = payload.get("action") in READ_ACTIONS
    attempts = 2 if is_read else 1

    response = None

    for attempt in range(attempts):

        try:
            response = _post_once(payload)
            break

        except (requests.Timeout, requests.ConnectionError) as e:

            print(f"Таблица: попытка {attempt + 1} не удалась:", repr(e))

            if attempt == attempts - 1:

                if isinstance(e, requests.Timeout):
                    return ("Таблица отвечает слишком долго. Попробуйте ещё раз.", None)

                return ("Не удалось связаться с таблицей.", None)

        except requests.RequestException as e:
            print("Ошибка соединения с таблицей:", repr(e))
            return ("Не удалось связаться с таблицей.", None)

    if DEBUG:
        print("Apps Script HTTP:", response.status_code)
        print("Apps Script response:", response.text[:2000])

    if response.status_code != 200:
        print("Apps Script HTTP ошибка:", response.status_code, response.text[:1000])
        return (f"Ошибка таблицы. HTTP {response.status_code}", None)

    raw = response.text.strip().lstrip("\ufeff")

    if not raw:
        return ("Таблица вернула пустой ответ.", None)

    try:
        data = response.json()
    except ValueError:
        print("Apps Script вернул НЕ JSON:")
        print(raw[:2000])
        return (
            "Ошибка ответа таблицы. Проверьте SCRIPT_URL "
            "и развёртывание Apps Script.",
            None
        )

    if data.get("ok") is False:
        return (str(data.get("message", "Неизвестная ошибка таблицы.")), None)

    message = data.get("message")

    if message is not None:
        return (str(message), data)

    return ("Таблица вернула пустой ответ.", None)


def call_sheet(payload):
    return call_sheet_ex(payload)[0]

# ==========================================================
# ПРОГРЕВ APPS SCRIPT (чтобы не "засыпал")
# ==========================================================

def keepalive_loop():
    while True:
        try:
            call_sheet_ex({"action": "ping"})
        except Exception as e:
            print("Keepalive:", repr(e))
        time.sleep(KEEPALIVE_INTERVAL)

# ==========================================================
# ВЫДАТЬ / ЗАБРАТЬ
# ==========================================================

def cmd_give(args, sign):

    if len(args) != 2 or not re.fullmatch(r"\d+", args[1]):
        command = "/выдать" if sign > 0 else "/забрать"
        return f"Формат: {command} Nick_Name 5"

    amount = int(args[1])

    if amount <= 0:
        return "Количество баллов должно быть больше 0."

    return call_sheet({
        "action": "points",
        "nick": args[0],
        "delta": sign * amount
    })

# ==========================================================
# ОПРЕДЕЛЕНИЕ VK ID
# ==========================================================

def resolve_user(raw):

    raw = raw.strip()

    match = re.fullmatch(r"\[id(\d+)\|.*\]", raw)

    if match:
        return int(match.group(1))

    username = raw.lstrip("@").strip()

    if not username:
        return None

    try:
        users = vk.users.get(user_ids=username)

        if not users:
            return None

        return users[0]["id"]

    except vk_api.ApiError as e:
        print("Ошибка поиска VK:", repr(e))
        return None

# ==========================================================
# ПРИВЯЗАТЬ
# ==========================================================

def cmd_bind(args):

    if len(args) != 4:
        return "Формат:\n/привязать @username Nick_Name по 25/09"

    username = args[0]
    nick = args[1]
    position = args[2].lower()
    date = args[3]

    if position not in ("по", "зо"):
        return "Должность должна быть «по» или «зо»."

    if not re.fullmatch(r"\d{2}/\d{2}", date):
        return "Дата должна быть в формате дд/мм."

    user_id = resolve_user(username)

    if user_id is None:
        return f"Пользователь {username} не найден в VK."

    return call_sheet({
        "action": "bind",
        "nick": nick,
        "position": position,
        "date": date,
        "link": f"https://vk.com/id{user_id}"
    })

# ==========================================================
# УБРАТЬ
# ==========================================================

def cmd_remove(args):

    if len(args) != 1:
        return "Формат: /убрать @username"

    user_id = resolve_user(args[0])

    if user_id is None:
        return f"Пользователь {args[0]} не найден в VK."

    return call_sheet({
        "action": "remove",
        "link": f"https://vk.com/id{user_id}"
    })

# ==========================================================
# СПИСОК
# ==========================================================

def cmd_list(args):

    if args:
        return "Формат: /список"

    return call_sheet({"action": "list"})

# ==========================================================
# РАЗОСЛАТЬ
# ==========================================================

def cmd_broadcast(raw_text):

    parts = raw_text.strip().split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        return "Формат: /разослать текст"

    message = parts[1].strip()

    text, data = call_sheet_ex({"action": "all_links"})

    if data is None:
        return text

    ids = []

    for link in data.get("links", []):
        m = re.search(r"vk\.com/id(\d+)", link)
        if m:
            ids.append(int(m.group(1)))

    # убрать повторы, сохранив порядок
    ids = list(dict.fromkeys(ids))

    if not ids:
        return "В таблице нет пользователей для рассылки."

    sent = 0
    failed = 0

    for uid in ids:

        try:
            vk.messages.send(user_id=uid, message=message, random_id=0)
            sent += 1

        except Exception as e:
            failed += 1
            print(f"Рассылка: не удалось отправить {uid}:", repr(e))

        time.sleep(0.1)

    return f"Рассылка завершена. Отправлено: {sent}, не доставлено: {failed}."

# ==========================================================
# БАЛЛЫ ТЕКУЩЕГО ПОЛЬЗОВАТЕЛЯ
# ==========================================================

def cmd_points(user_id):

    return call_sheet({
        "action": "my_points",
        "link": f"https://vk.com/id{user_id}"
    })

# ==========================================================
# HELP
# ==========================================================

USER_HELP = (
    "Ваши команды:\n"
    "/баллы\n"
    "/магазин\n"
    "/купить"
)

ADMIN_HELP = (
    "Все команды:\n\n"
    "Для всех:\n"
    "/баллы — узнать свои баллы\n"
    "/магазин — список товаров\n"
    "/купить (номер) — купить товар\n"
    "/help — список команд\n\n"
    "Для администраторов:\n"
    "/выдать Nick_Name 5 — выдать баллы\n"
    "/забрать Nick_Name 5 — забрать баллы\n"
    "/привязать @username Nick_Name по 25/09 — привязать игрока\n"
    "/убрать @username — удалить игрока из таблицы\n"
    "/список — список участников с баллами\n"
    "/+магазин 100 товар — добавить товар в магазин\n"
    "/-магазин 3 — удалить товар из магазина по номеру"
)


def cmd_help(user_id):
    return ADMIN_HELP if user_id in ALLOWED_IDS else USER_HELP

# ==========================================================
# МАГАЗИН (с кэшем на 30 сек)
# ==========================================================

SHOP_CACHE_TTL = 30
_shop_cache = {"text": None, "time": 0.0}


def shop_cache_clear():
    _shop_cache["text"] = None


def cmd_shop():

    now = time.time()

    if _shop_cache["text"] and now - _shop_cache["time"] < SHOP_CACHE_TTL:
        return _shop_cache["text"]

    text, data = call_sheet_ex({"action": "shop_list"})

    # кэшируем только успешный ответ
    if data is not None:
        _shop_cache["text"] = text
        _shop_cache["time"] = now

    return text


def cmd_buy(args, user_id):

    if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
        return "Формат: /купить 1"

    text, data = call_sheet_ex({
        "action": "buy",
        "link": f"https://vk.com/id{user_id}",
        "item": int(args[0])
    })

    purchase = (data or {}).get("purchase")

    if purchase:
        # уведомление админам в отдельном потоке, чтобы не задерживать ответ игроку
        executor.submit(
            notify_admins,
            f"[id{user_id}|@{purchase['nick']}] "
            f"({purchase['nick']}) купил №{purchase['num']}: "
            f"{purchase['item']} ({purchase['price']} баллов). "
            f"Выдайте ему."
        )

    return text


def cmd_shop_add(args):

    if len(args) < 2 or not re.fullmatch(r"\d+", args[0]):
        return "Формат: /+магазин 100 что выдать"

    price = int(args[0])

    if price <= 0:
        return "Цена должна быть больше 0."

    result = call_sheet({
        "action": "shop_add",
        "price": price,
        "name": " ".join(args[1:])
    })

    shop_cache_clear()

    return result


def cmd_shop_remove(args):

    if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
        return "Формат: /-магазин 3"

    result = call_sheet({
        "action": "shop_remove",
        "item": int(args[0])
    })

    shop_cache_clear()

    return result

# ==========================================================
# ОБРАБОТКА АДМИНСКИХ КОМАНД
# ==========================================================

def handle(text, user_id):

    parts = text.strip().split()

    if not parts:
        return None

    command = parts[0].lower()
    args = parts[1:]

    if command == "/выдать":
        return cmd_give(args, +1)

    if command == "/забрать":
        return cmd_give(args, -1)

    if command == "/привязать":
        return cmd_bind(args)

    if command == "/убрать":
        return cmd_remove(args)

    if command == "/список":
        return cmd_list(args)

    if command == "/+магазин":
        return cmd_shop_add(args)

    if command == "/-магазин":
        return cmd_shop_remove(args)

    if command == "/разослать":
        return cmd_broadcast(text)

    return None

# ==========================================================
# ОБРАБОТКА СОБЫТИЯ VK
# ==========================================================

def process_event(event):

    if DEBUG:
        print("Событие:", event.type)

    if event.type != VkBotEventType.MESSAGE_NEW:
        return

    msg = event.obj.message

    text = msg.get("text", "").strip()
    peer_id = msg["peer_id"]
    from_id = msg["from_id"]

    if DEBUG:
        print(f"Сообщение от {from_id}: {text!r}")

    if not text.startswith("/"):
        return

    # ======================================================
    # ПУБЛИЧНЫЕ КОМАНДЫ (доступны всем)
    # ======================================================

    parts = text.split()
    command = parts[0].lower()
    args = parts[1:]

    is_public = True
    public_answer = None

    try:

        if command == "/баллы":
            public_answer = "Формат: /баллы" if args else cmd_points(from_id)

        elif command in ("/help", "/хелп"):
            public_answer = cmd_help(from_id)

        elif command == "/магазин":
            public_answer = cmd_shop()

        elif command == "/купить":
            public_answer = cmd_buy(args, from_id)

        elif command == "/я":
            public_answer = f"Ваш VK ID: {from_id}"

        else:
            is_public = False

    except Exception as e:
        print("Ошибка публичной команды:", repr(e))
        public_answer = "Произошла ошибка. Попробуйте позже."

    if is_public:

        if public_answer:
            reply(peer_id, public_answer)

        return

    # ======================================================
    # ВСЕ ОСТАЛЬНЫЕ КОМАНДЫ — только ALLOWED_IDS
    # ======================================================

    if from_id not in ALLOWED_IDS:
        return

    try:
        answer = handle(text, from_id)
    except Exception as e:
        print("Ошибка команды:", repr(e))
        answer = "Произошла ошибка при выполнении команды."

    if answer:
        reply(peer_id, answer)


def safe_process(event):
    try:
        process_event(event)
    except Exception as e:
        print("Ошибка обработки:", repr(e))

# ==========================================================
# ЗАПУСК БОТА
# ==========================================================

def main():

    threading.Thread(target=keepalive_loop, daemon=True).start()

    while True:

        try:
            longpoll = VkBotLongPoll(vk_session, GROUP_ID)

            print("Бот запущен. Жду сообщения...")

            for event in longpoll.listen():
                # каждое событие обрабатывается в своём потоке —
                # медленная команда больше не блокирует остальные
                executor.submit(safe_process, event)

        except vk_api.exceptions.ApiError as e:
            print("VK API:", repr(e))
            time.sleep(3)

        except Exception as e:
            print("Соединение прервано:", repr(e))
            time.sleep(2)

# ==========================================================
# START
# ==========================================================

if __name__ == "__main__":
    main()
