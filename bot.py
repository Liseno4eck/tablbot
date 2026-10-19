import os
import re
import json
import time
import threading
import vk_api

from concurrent.futures import ThreadPoolExecutor
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.utils import get_random_id

# ==========================================================
# НАСТРОЙКИ
# ==========================================================

# Токен лучше задавать переменной окружения VK_TOKEN (в Pterodactyl — в Startup/Variables).
VK_TOKEN = os.environ.get("VK_TOKEN", "")
if not VK_TOKEN:
    raise SystemExit("VK_TOKEN пустой: задайте переменную окружения VK_TOKEN")

GROUP_ID = 241841230

# VK ID пользователей, которым доступны админские команды
ALLOWED_IDS = {
    875762552,
    857698829,
    200005206671,
    708897961
}

# Админы, прописанные в коде: командой их снять нельзя
BASE_ADMIN_IDS = set(ALLOWED_IDS)

# Единственное хранилище: локальный JSON-файл рядом с ботом.
# На Pterodactyl он переживает перезапуски. Не удаляйте data.json при передеплое!
DATA_FILE = os.environ.get("DATA_FILE", "data.json")

DEBUG = False

# ==========================================================
# ПОДКЛЮЧЕНИЕ К VK
# ==========================================================

vk_session = vk_api.VkApi(token=VK_TOKEN)
vk = vk_session.get_api()

executor = ThreadPoolExecutor(max_workers=8)

# ==========================================================
# ХРАНИЛИЩЕ (data.json)
#
# {
#   "players": {"nick_lower": {"nick": "Nick_Name", "vk_id": 123, "points": 0}},
#   "shop":    [{"name": "...", "price": 100}],
#   "admins":  [111, 222]          # выданные через /вд
# }
# ==========================================================

lock = threading.RLock()
data = {"players": {}, "shop": [], "admins": []}


def save_data():
    """Атомарная запись: сначала во временный файл, потом подмена."""
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def load_data():
    if not os.path.exists(DATA_FILE):
        save_data()
        return

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception as e:
        # не затираем битый файл — делаем копию и стартуем с пустого
        print("Не удалось прочитать data.json:", repr(e))
        try:
            os.replace(DATA_FILE, DATA_FILE + ".broken")
        except Exception:
            pass
        save_data()
        return

    data["players"] = loaded.get("players", {})
    data["shop"] = loaded.get("shop", [])
    data["admins"] = loaded.get("admins", [])

    ALLOWED_IDS.update(int(x) for x in data["admins"])


def find_by_nick(nick):
    return data["players"].get(nick.lower())


def find_by_id(vk_id):
    for p in data["players"].values():
        if p["vk_id"] == vk_id:
            return p
    return None

# ==========================================================
# ОТПРАВКА СООБЩЕНИЙ
# ==========================================================

def reply(peer_id, text):
    try:
        vk.messages.send(peer_id=peer_id, message=str(text), random_id=get_random_id())
    except Exception as e:
        print("Ошибка отправки сообщения:", repr(e))


def notify_admins(text):
    for admin_id in list(ALLOWED_IDS):
        try:
            vk.messages.send(user_id=admin_id, message=text, random_id=get_random_id())
        except Exception as e:
            print(f"Не удалось написать {admin_id}:", repr(e))

# ==========================================================
# ОПРЕДЕЛЕНИЕ VK ID
# ==========================================================

def resolve_user(raw):

    raw = raw.strip()

    match = re.fullmatch(r"\[id(\d+)\|.*\]", raw)

    if match:
        return int(match.group(1))

    username = raw.lstrip("@").strip()

    # ссылка вида https://vk.com/durov
    username = re.sub(r"^(https?://)?(m\.)?vk\.com/", "", username)

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
# ВЫДАТЬ / ЗАБРАТЬ
# ==========================================================

def cmd_give(args, sign):

    if len(args) != 2 or not re.fullmatch(r"\d+", args[1]):
        command = "/выдать" if sign > 0 else "/забрать"
        return f"Формат: {command} Nick_Name 5"

    amount = int(args[1])

    if amount <= 0:
        return "Количество баллов должно быть больше 0."

    with lock:
        player = find_by_nick(args[0])

        if player is None:
            return f"Игрок {args[0]} не найден."

        if sign < 0 and player["points"] < amount:
            return (
                f"У {player['nick']} только {player['points']} баллов, "
                f"забрать {amount} нельзя."
            )

        player["points"] += sign * amount
        save_data()

        verb = "Выдано" if sign > 0 else "Забрано"
        return f"{verb} {amount} баллов. У {player['nick']} теперь {player['points']}."

# ==========================================================
# ПРИВЯЗАТЬ
#   /привязать @username Nick_Name
# ==========================================================

def cmd_bind(args):

    if len(args) != 2:
        return "Формат: /привязать @username Nick_Name"

    username, nick = args

    user_id = resolve_user(username)

    if user_id is None:
        return f"Пользователь {username} не найден в VK."

    with lock:
        if find_by_nick(nick) is not None:
            return f"Ник {nick} уже занят."

        existing = find_by_id(user_id)

        if existing is not None:
            return f"Этот пользователь уже привязан к нику {existing['nick']}."

        data["players"][nick.lower()] = {
            "nick": nick,
            "vk_id": user_id,
            "points": 0
        }
        save_data()

    return f"Игрок {nick} привязан к https://vk.com/id{user_id}."

# ==========================================================
# ОТВЯЗАТЬ
#   /отвязать @username
#   /отвязать Nick_Name
# ==========================================================

def cmd_unbind(args):

    if len(args) != 1:
        return "Формат: /отвязать @username или /отвязать Nick_Name"

    target = args[0]

    with lock:
        # сначала пробуем как ник
        player = find_by_nick(target)

        # потом как пользователя VK
        if player is None:
            user_id = resolve_user(target)

            if user_id is not None:
                player = find_by_id(user_id)

        if player is None:
            return f"Игрок {target} не найден."

        del data["players"][player["nick"].lower()]
        save_data()

        return f"Игрок {player['nick']} отвязан."

# ==========================================================
# ИЗМЕНИТЬ (смена ника)
#   /изменить Старый_Ник Новый_Ник
# ==========================================================

def cmd_change(args):

    if len(args) != 2:
        return "Формат: /изменить Старый_Ник Новый_Ник"

    old, new = args

    with lock:
        player = find_by_nick(old)

        if player is None:
            return f"Игрок {old} не найден."

        if old.lower() != new.lower() and find_by_nick(new) is not None:
            return f"Ник {new} уже занят."

        del data["players"][player["nick"].lower()]
        player["nick"] = new
        data["players"][new.lower()] = player
        save_data()

    return f"Ник изменён: {old} → {new}."

# ==========================================================
# ВД — выдать полный доступ админа
# ==========================================================

def cmd_grant_admin(args):

    if len(args) != 1:
        return "Формат: /вд @username"

    user_id = resolve_user(args[0])

    if user_id is None:
        return f"Пользователь {args[0]} не найден в VK."

    with lock:
        if user_id in ALLOWED_IDS:
            return f"[id{user_id}|Пользователь] уже администратор."

        ALLOWED_IDS.add(user_id)

        if user_id not in data["admins"]:
            data["admins"].append(user_id)

        save_data()

    return f"[id{user_id}|Пользователь] теперь администратор."


def cmd_revoke_admin(args):

    if len(args) != 1:
        return "Формат: /снятьвд @username"

    user_id = resolve_user(args[0])

    if user_id is None:
        return f"Пользователь {args[0]} не найден в VK."

    if user_id in BASE_ADMIN_IDS:
        return "Этого админа нельзя снять командой, он прописан в коде."

    with lock:
        if user_id not in ALLOWED_IDS:
            return f"[id{user_id}|Пользователь] не администратор."

        ALLOWED_IDS.discard(user_id)

        if user_id in data["admins"]:
            data["admins"].remove(user_id)

        save_data()

    return f"У [id{user_id}|пользователя] забран доступ админа."

# ==========================================================
# СПИСОК
# ==========================================================

def cmd_list(args):

    if args:
        return "Формат: /список"

    with lock:
        players = sorted(
            data["players"].values(),
            key=lambda p: (-p["points"], p["nick"].lower())
        )

    if not players:
        return "Список пуст."

    lines = [
        f"{i}. {p['nick']} — {p['points']} (vk.com/id{p['vk_id']})"
        for i, p in enumerate(players, 1)
    ]

    return "Участники:\n" + "\n".join(lines)

# ==========================================================
# РАЗОСЛАТЬ
# ==========================================================

def cmd_broadcast(raw_text):

    parts = raw_text.strip().split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        return "Формат: /разослать текст"

    message = parts[1].strip()

    with lock:
        ids = list(dict.fromkeys(p["vk_id"] for p in data["players"].values()))

    if not ids:
        return "Нет привязанных пользователей для рассылки."

    sent = 0
    failed = 0

    for uid in ids:

        try:
            vk.messages.send(user_id=uid, message=message, random_id=get_random_id())
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

    with lock:
        player = find_by_id(user_id)

        if player is None:
            return "Вы не привязаны к нику. Обратитесь к администратору."

        return f"{player['nick']}, у вас {player['points']} баллов."

# ==========================================================
# МАГАЗИН
# ==========================================================

def cmd_shop():

    with lock:
        shop = list(data["shop"])

    if not shop:
        return "Магазин пуст."

    lines = [
        f"{i}. {item['name']} — {item['price']} баллов"
        for i, item in enumerate(shop, 1)
    ]

    return "Магазин:\n" + "\n".join(lines) + "\n\nКупить: /купить номер"


def cmd_buy(args, user_id):

    if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
        return "Формат: /купить 1"

    num = int(args[0])

    with lock:
        player = find_by_id(user_id)

        if player is None:
            return "Вы не привязаны к нику. Обратитесь к администратору."

        if num < 1 or num > len(data["shop"]):
            return "Товара с таким номером нет."

        item = data["shop"][num - 1]

        if player["points"] < item["price"]:
            return (
                f"Не хватает баллов. Нужно {item['price']}, "
                f"у вас {player['points']}."
            )

        player["points"] -= item["price"]
        save_data()

        nick = player["nick"]
        left = player["points"]

    # уведомление админам в отдельном потоке, чтобы не задерживать ответ игроку
    executor.submit(
        notify_admins,
        f"[id{user_id}|@{nick}] ({nick}) купил №{num}: "
        f"{item['name']} ({item['price']} баллов). Выдайте ему."
    )

    return f"Вы купили: {item['name']}. Осталось баллов: {left}."


def cmd_shop_add(args):

    if len(args) < 2 or not re.fullmatch(r"\d+", args[0]):
        return "Формат: /+магазин 100 что выдать"

    price = int(args[0])

    if price <= 0:
        return "Цена должна быть больше 0."

    name = " ".join(args[1:])

    with lock:
        data["shop"].append({"name": name, "price": price})
        save_data()
        num = len(data["shop"])

    return f"Товар добавлен под номером {num}: {name} — {price} баллов."


def cmd_shop_remove(args):

    if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
        return "Формат: /-магазин 3"

    num = int(args[0])

    with lock:
        if num < 1 or num > len(data["shop"]):
            return "Товара с таким номером нет."

        item = data["shop"].pop(num - 1)
        save_data()

    return f"Товар удалён: {item['name']}."

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
    "/привязать @username Nick_Name — привязать игрока\n"
    "/отвязать @username или Nick_Name — отвязать игрока\n"
    "/изменить Старый Новый — сменить ник\n"
    "/список — список участников с баллами\n"
    "/разослать текст — рассылка привязанным игрокам\n"
    "/+магазин 100 товар — добавить товар в магазин\n"
    "/-магазин 3 — удалить товар из магазина по номеру\n"
    "/вд @username — выдать полный доступ админа\n"
    "/снятьвд @username — забрать доступ админа"
)


def cmd_help(user_id):
    return ADMIN_HELP if user_id in ALLOWED_IDS else USER_HELP

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

    if command in ("/отвязать", "/убрать"):
        return cmd_unbind(args)

    if command == "/изменить":
        return cmd_change(args)

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

    load_data()

    print(
        f"Данные загружены. Игроков: {len(data['players'])}, "
        f"товаров: {len(data['shop'])}, админов: {len(ALLOWED_IDS)}"
    )

    while True:

        try:
            longpoll = VkBotLongPoll(vk_session, GROUP_ID)

            print("Бот запущен. Жду сообщения...")

            for event in longpoll.listen():
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
