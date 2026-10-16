import os
import re
import json
import time
import threading
import requests
import vk_api

from concurrent.futures import ThreadPoolExecutor
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

# ==========================================================
# НАСТРОЙКИ
# ==========================================================

# Токен лучше задавать переменной окружения VK_TOKEN (в Pterodactyl — в Startup/Variables).
VK_TOKEN = os.environ.get("VK_TOKEN", "")
if not VK_TOKEN:
    raise SystemExit("VK_TOKEN пустой: задайте переменную окружения VK_TOKEN")

GROUP_ID = 241841230

SCRIPT_URL = "https://script.google.com/macros/s/AKfycbxuPmZE0ApEB3rq_6Nj1ZXiuWFkdo7TVpdnPAbHglEFkNkeUPsyFU0g22aIi3SaCNMt/exec"

# Должен совпадать с SECRET в Google Apps Script
SCRIPT_SECRET = os.environ.get("SCRIPT_SECRET", "724422")

# VK ID пользователей, которым доступны админские команды
ALLOWED_IDS = {
    875762552,
    857698829,
    200005206671,
    708897961
}

# Админы, прописанные в коде: командой их снять нельзя
BASE_ADMIN_IDS = set(ALLOWED_IDS)

# Локальный файл — только запасной вариант на случай, если таблица недоступна при старте.
# Основное хранилище админов — Google Apps Script (переживает перезапуски и передеплой).
ADMINS_FILE = "admins.json"

DEBUG = False

# Таймаут запроса к таблице (сек) и интервал "прогрева" скрипта
SHEET_TIMEOUT = 25
KEEPALIVE_INTERVAL = 240

# Действия, которые только читают (их безопасно повторять при таймауте)
READ_ACTIONS = {"list", "my_points", "all_links", "shop_list", "ping", "admins_get"}

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
    for admin_id in list(ALLOWED_IDS):
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
# ХРАНЕНИЕ АДМИНОВ (Google Apps Script + локальный файл как запас)
# ==========================================================

def _apply_admins(extra_ids):
    """Обновляет ALLOWED_IDS на месте: админы из кода + extra_ids."""
    new_ids = set(BASE_ADMIN_IDS) | set(extra_ids)
    ALLOWED_IDS.difference_update(ALLOWED_IDS - new_ids)
    ALLOWED_IDS.update(new_ids)


def load_admins(use_file_fallback=False):
    """
    Загружает админов из таблицы. Возвращает True, если удалось.
    Если таблица недоступна и use_file_fallback=True — берёт локальный файл.
    """

    try:
        text, data = call_sheet_ex({"action": "admins_get"})

        if data is not None and isinstance(data.get("admins"), list):
            _apply_admins(int(x) for x in data["admins"])
            return True

        print("Не удалось получить админов из таблицы:", text)

    except Exception as e:
        print("Ошибка загрузки админов из таблицы:", repr(e))

    if use_file_fallback:
        try:
            with open(ADMINS_FILE, "r", encoding="utf-8") as f:
                _apply_admins(int(x) for x in json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:
            print("Не удалось загрузить admins.json:", repr(e))

    return False


def save_admins():
    """Сохраняет админов в таблицу (и в локальный файл как запас). True — если в таблицу записалось."""

    ids = sorted(ALLOWED_IDS)
    saved = False

    try:
        text, data = call_sheet_ex({"action": "admins_set", "admins": ids})

        if data is not None:
            saved = True
        else:
            print("Не удалось сохранить админов в таблицу:", text)

    except Exception as e:
        print("Ошибка сохранения админов в таблицу:", repr(e))

    try:
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump(ids, f)
    except Exception as e:
        print("Не удалось сохранить admins.json:", repr(e))

    return saved

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
# ИЗМЕНИТЬ
#   /изменить Старый Новый                -> только ник
#   /изменить Старый Новый по 25/09       -> ник + должность + дата
#   /изменить Ник по 25/09                -> только должность + дата
# ==========================================================

CHANGE_FORMAT = (
    "Формат:\n"
    "/изменить Старый_Ник Новый_Ник — сменить ник\n"
    "/изменить Старый_Ник Новый_Ник по 25/09 — сменить ник, должность и дату\n"
    "/изменить Ник по 25/09 — сменить только должность и дату"
)


def cmd_change(args):

    # /изменить Старый Новый
    if len(args) == 2:
        return call_sheet({
            "action": "change",
            "nick": args[0],
            "new_nick": args[1]
        })

    # /изменить Ник по 25/09
    # /изменить Старый Новый по 25/09
    if len(args) in (3, 4):

        if len(args) == 3:
            nick, new_nick, position, date = args[0], None, args[1], args[2]
        else:
            nick, new_nick, position, date = args[0], args[1], args[2], args[3]

        position = position.lower()

        if position not in ("по", "зо"):
            return "Должность должна быть «по» или «зо»."

        if not re.fullmatch(r"\d{2}/\d{2}", date):
            return "Дата должна быть в формате дд/мм."

        payload = {
            "action": "change",
            "nick": nick,
            "position": position,
            "date": date
        }

        if new_nick:
            payload["new_nick"] = new_nick

        return call_sheet(payload)

    return CHANGE_FORMAT

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
# ВД — выдать полный доступ админа
# ==========================================================

ADMINS_UNAVAILABLE = (
    "Не удалось связаться с таблицей, список админов не изменён. "
    "Попробуйте ещё раз."
)

ADMINS_SAVE_FAILED = (
    "Не удалось сохранить список админов в таблицу. "
    "Изменение действует только до перезапуска бота. Попробуйте ещё раз."
)


def cmd_grant_admin(args):

    if len(args) != 1:
        return "Формат: /вд @username"

    user_id = resolve_user(args[0])

    if user_id is None:
        return f"Пользователь {args[0]} не найден в VK."

    # подтягиваем актуальный список, чтобы не затереть чужие изменения
    if not load_admins():
        return ADMINS_UNAVAILABLE

    if user_id in ALLOWED_IDS:
        return f"[id{user_id}|Пользователь] уже администратор."

    ALLOWED_IDS.add(user_id)

    if not save_admins():
        return ADMINS_SAVE_FAILED

    return f"[id{user_id}|Пользователь] теперь администратор."


def cmd_revoke_admin(args):

    if len(args) != 1:
        return "Формат: /снятьвд @username"

    user_id = resolve_user(args[0])

    if user_id is None:
        return f"Пользователь {args[0]} не найден в VK."

    if user_id in BASE_ADMIN_IDS:
        return "Этого админа нельзя снять командой, он прописан в коде."

    if not load_admins():
        return ADMINS_UNAVAILABLE

    if user_id not in ALLOWED_IDS:
        return f"[id{user_id}|Пользователь] не администратор."

    ALLOWED_IDS.discard(user_id)

    if not save_admins():
        return ADMINS_SAVE_FAILED

    return f"У [id{user_id}|пользователя] забран доступ админа."

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
    "/изменить Старый Новый — сменить ник\n"
    "/изменить Старый Новый по 25/09 — сменить ник, должность и дату\n"
    "/изменить Ник по 25/09 — сменить только должность и дату\n"
    "/убрать @username — удалить игрока из таблицы\n"
    "/список — список участников с баллами\n"
    "/+магазин 100 товар — добавить товар в магазин\n"
    "/-магазин 3 — удалить товар из магазина по номеру\n"
    "/вд @username — выдать полный доступ админа\n"
    "/снятьвд @username — забрать доступ админа"
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

    if command == "/изменить":
        return cmd_change(args)

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

    if command == "/вд":
        return cmd_grant_admin(args)

    if command == "/снятьвд":
        return cmd_revoke_admin(args)

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

    # подтягиваем админов, выданных через /вд (из таблицы; файл — только запас)
    if load_admins(use_file_fallback=True):
        print(f"Админы загружены из таблицы. Всего админов: {len(ALLOWED_IDS)}")
    else:
        print(f"Таблица недоступна, админы взяты из запасного файла. Всего: {len(ALLOWED_IDS)}")

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
