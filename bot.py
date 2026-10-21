import os
import re
import json
import time
import threading
import urllib.request
import urllib.error
import vk_api
from concurrent.futures import ThreadPoolExecutor
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.utils import get_random_id
# ==========================================================
# НАСТРОЙКИ
# ==========================================================
VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
if not VK_TOKEN:
    raise SystemExit("VK_TOKEN пустой: задайте переменную окружения VK_TOKEN")
GROUP_ID = 241841230
ALLOWED_IDS = {
    875762552,
    857698829,
    200005206671,
    708897961,
}
BASE_ADMIN_IDS = set(ALLOWED_IDS)
DATA_FILE = os.environ.get("DATA_FILE", "data.json")
GIST_TOKEN_FALLBACK = ""
GIST_ID_FALLBACK = ""
def _clean_gist_id(value):
    value = (value or "").strip()
    match = re.search(r"gist\.github\.com/(?:[^/]+/)?([0-9a-fA-F]+)", value)
    if match:
        return match.group(1)
    return value
GIST_TOKEN = (os.environ.get("GIST_TOKEN", "") or GIST_TOKEN_FALLBACK).strip()
GIST_ID = _clean_gist_id(os.environ.get("GIST_ID", "") or GIST_ID_FALLBACK)
GIST_FILENAME = "data.json"
GIST_ENABLED = bool(GIST_TOKEN and GIST_ID)
ON_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
DEBUG = False
# ==========================================================
# ПОДКЛЮЧЕНИЕ К VK
# ==========================================================
vk_session = vk_api.VkApi(token=VK_TOKEN)
vk = vk_session.get_api()
executor = ThreadPoolExecutor(max_workers=8)
# ==========================================================
# ХРАНИЛИЩЕ
# players: nick_lower -> {nick, vk_id, points, vacation, waiting_vacation}
# ==========================================================
lock = threading.RLock()
save_lock = threading.Lock()
data = {"players": {}, "shop": [], "admins": []}
def _http_error_text(e):
    try:
        if hasattr(e, "read"):
            return e.read().decode("utf-8", "ignore")
    except Exception:
        pass
    return ""
def _gist_request(method, url, body=None):
    headers = {
        "Authorization": "Bearer " + GIST_TOKEN,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "vk-points-bot",
    }
    raw = None
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=raw, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8")
def gist_load():
    if not GIST_ENABLED:
        return None
    try:
        text = _gist_request("GET", "https://api.github.com/gists/" + GIST_ID)
        info = json.loads(text)
        files = info.get("files") or {}
        file_info = files.get(GIST_FILENAME)
        if file_info is None:
            print("В гисте нет файла " + GIST_FILENAME + ". Есть файлы: " + str(list(files.keys())))
            return {}
        content = file_info.get("content") or ""
        if file_info.get("truncated") and file_info.get("raw_url"):
            req = urllib.request.Request(
                file_info["raw_url"],
                headers={"Authorization": "Bearer " + GIST_TOKEN, "User-Agent": "vk-points-bot"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                content = resp.read().decode("utf-8")
        content = content.strip()
        if not content:
            return {}
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("содержимое Gist должно быть JSON-объектом")
        return parsed
    except Exception as e:
        print("Не удалось загрузить данные из Gist:", repr(e), _http_error_text(e))
        return None
def gist_load_with_retry(attempts=3, delay=5):
    for attempt in range(1, attempts + 1):
        result = gist_load()
        if result is not None:
            return result
        if attempt < attempts:
            print("Повтор загрузки из Gist через " + str(delay) + " сек (" + str(attempt) + "/" + str(attempts) + ")...")
            time.sleep(delay)
    return None
def _gist_push(payload):
    _gist_request(
        "PATCH",
        "https://api.github.com/gists/" + GIST_ID,
        {"files": {GIST_FILENAME: {"content": payload}}},
    )
def gist_save(payload, attempts=4):
    if not GIST_ENABLED:
        return True
    delays = [1, 2, 4]
    for attempt in range(1, attempts + 1):
        try:
            _gist_push(payload)
            print("Данные сохранены в Gist.")
            return True
        except Exception as e:
            print("Ошибка сохранения в Gist (попытка " + str(attempt) + "/" + str(attempts) + "):", repr(e), _http_error_text(e))
            if attempt < attempts:
                time.sleep(delays[min(attempt - 1, len(delays) - 1)])
    return False
def _save_local(payload):
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
            os.replace(tmp, DATA_FILE)
        return True
    except Exception as e:
        print("Ошибка записи локального файла:", repr(e))
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False
def save_data():
    with save_lock:
        with lock:
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            _save_local(payload)
            if GIST_ENABLED:
                return gist_save(payload)
            return True
def _has_content(d):
    return bool(d) and any(d.get(key) for key in ("players", "shop", "admins"))
def _read_local():
    if not os.path.exists(DATA_FILE):
        return None
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            parsed = json.load(f)
            if not isinstance(parsed, dict):
                raise ValueError("data.json должен быть JSON-объектом")
            return parsed
    except Exception as e:
        print("Не удалось прочитать локальный data.json:", repr(e))
        try:
            os.replace(DATA_FILE, DATA_FILE + ".broken")
        except Exception:
            pass
        return None
def _normalize_loaded(loaded):
    players = {}
    raw_players = loaded.get("players")
    if isinstance(raw_players, dict):
        for key, value in raw_players.items():
            if not isinstance(value, dict):
                continue
            nick = str(value.get("nick") or key).strip()
            try:
                vk_id = int(value.get("vk_id"))
            except (TypeError, ValueError):
                continue
            try:
                points = int(value.get("points", 0))
            except (TypeError, ValueError):
                points = 0
            if not nick or vk_id <= 0:
                continue
            players[nick.lower()] = {
                "nick": nick,
                "vk_id": vk_id,
                "points": points,
                "vacation": value.get("vacation"),
                "otgul": int(value.get("otgul", 0) or 0),
                "waiting_vacation": bool(value.get("waiting_vacation", False)),
            }
    shop = []
    raw_shop = loaded.get("shop")
    if isinstance(raw_shop, list):
        for item in raw_shop:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            try:
                price = int(item.get("price", 0))
            except (TypeError, ValueError):
                continue
            if name and price > 0:
                shop.append({"name": name, "price": price})
    admins = []
    raw_admins = loaded.get("admins")
    if isinstance(raw_admins, list):
        for value in raw_admins:
            try:
                admins.append(int(value))
            except (TypeError, ValueError):
                pass
    return {"players": players, "shop": shop, "admins": admins}
def load_data():
    local = _read_local()
    remote = gist_load_with_retry() if GIST_ENABLED else None
    if GIST_ENABLED and remote is None:
        raise SystemExit(
            "Не удалось прочитать данные из GitHub Gist. Бот остановлен, "
            "чтобы не перезаписать сохранённые данные пустым файлом. "
            "Проверьте GIST_TOKEN, GIST_ID и доступ к api.github.com."
        )
    need_push = False
    if GIST_ENABLED:
        if _has_content(remote):
            loaded = remote
            print("Данные загружены из GitHub Gist.")
        elif local is not None and _has_content(local):
            loaded = local
            need_push = True
            print("Gist пуст — данные взяты из локального файла и будут залиты в Gist.")
        else:
            loaded = {}
            need_push = True
            print("Данных нет, старт с пустого хранилища.")
    else:
        if local is not None:
            loaded = local
            print("Данные загружены из локального файла.")
        else:
            loaded = {}
            print("Данных нет, старт с пустого хранилища.")
            print("ВНИМАНИЕ: GIST_TOKEN/GIST_ID не заданы — данные хранятся только локально!")
    normalized = _normalize_loaded(loaded)
    data["players"] = normalized["players"]
    data["shop"] = normalized["shop"]
    data["admins"] = normalized["admins"]
    ALLOWED_IDS.clear()
    ALLOWED_IDS.update(BASE_ADMIN_IDS)
    ALLOWED_IDS.update(data["admins"])
    if need_push:
        if not save_data():
            raise SystemExit("Не удалось записать начальные данные в Gist. Бот остановлен.")
    elif GIST_ENABLED:
        with lock:
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            _save_local(payload)
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
            print("Не удалось написать " + str(admin_id) + ":", repr(e))
# ==========================================================
# ОПРЕДЕЛЕНИЕ VK ID
# ==========================================================
def resolve_user(raw):
    raw = raw.strip()
    match = re.fullmatch(r"\[id(\d+)\|.*\]", raw)
    if match:
        return int(match.group(1))
    match = re.fullmatch(r"(?:https?://)?(?:m\.)?vk\.com/id(\d+)", raw, re.IGNORECASE)
    if match:
        return int(match.group(1))
    username = raw.lstrip("@").strip()
    username = re.sub(r"^(https?://)?(m\.)?vk\.com/", "", username, flags=re.IGNORECASE)
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
        return "Формат: " + command + " Nick_Name 5"
    amount = int(args[1])
    if amount <= 0:
        return "Количество баллов должно быть больше 0."
    with lock:
        player = find_by_nick(args[0])
        if player is None:
            return "Игрок " + args[0] + " не найден."
        if sign < 0 and player["points"] < amount:
            return "У " + player["nick"] + " только " + str(player["points"]) + " баллов, забрать " + str(amount) + " нельзя."
        player["points"] += sign * amount
        nick = player["nick"]
        points = player["points"]
        save_data()
        verb = "Выдано" if sign > 0 else "Забрано"
        return verb + " " + str(amount) + " баллов. У " + nick + " теперь " + str(points) + "."
# ==========================================================
# ПРИВЯЗАТЬ
# ==========================================================
def cmd_bind(args):
    if len(args) != 2:
        return "Формат: /привязать @username Nick_Name"
    username, nick = args
    user_id = resolve_user(username)
    if user_id is None:
        return "Пользователь " + username + " не найден в VK."
    with lock:
        if find_by_nick(nick) is not None:
            return "Ник " + nick + " уже занят."
        existing = find_by_id(user_id)
        if existing is not None:
            return "Этот пользователь уже привязан к нику " + existing["nick"] + "."
        data["players"][nick.lower()] = {
            "nick": nick,
            "vk_id": user_id,
            "points": 0,
            "vacation": None,
            "otgul": 0,
            "waiting_vacation": False,
        }
        save_data()
        return "Игрок " + nick + " привязан к https://vk.com/id" + str(user_id) + "."
# ==========================================================
# ОТВЯЗАТЬ
# ==========================================================
def cmd_unbind(args):
    if len(args) != 1:
        return "Формат: /отвязать @username или /отвязать Nick_Name"
    target = args[0]
    with lock:
        player = find_by_nick(target)
        if player is not None:
            nick = player["nick"]
            del data["players"][nick.lower()]
            save_data()
            return "Игрок " + nick + " отвязан."
        user_id = resolve_user(target)
        if user_id is None:
            return "Игрок " + target + " не найден."
        with lock:
            player = find_by_id(user_id)
            if player is None:
                return "Игрок " + target + " не найден."
            nick = player["nick"]
            del data["players"][nick.lower()]
            save_data()
            return "Игрок " + nick + " отвязан."
# ==========================================================
# ИЗМЕНИТЬ (смена ника)
# ==========================================================
def cmd_change(args):
    if len(args) != 2:
        return "Формат: /изменить Старый_Ник Новый_Ник"
    old, new = args
    with lock:
        player = find_by_nick(old)
        if player is None:
            return "Игрок " + old + " не найден."
        if old.lower() != new.lower() and find_by_nick(new) is not None:
            return "Ник " + new + " уже занят."
        old_nick = player["nick"]
        del data["players"][old_nick.lower()]
        player["nick"] = new
        data["players"][new.lower()] = player
        save_data()
        return "Ник изменён: " + old_nick + " -> " + new + "."
# ==========================================================
# ВД — выдать полный доступ админа
# ==========================================================
def cmd_grant_admin(args):
    if len(args) != 1:
        return "Формат: /вд @username"
    user_id = resolve_user(args[0])
    if user_id is None:
        return "Пользователь " + args[0] + " не найден в VK."
    with lock:
        if user_id in ALLOWED_IDS:
            return "[id" + str(user_id) + "|Пользователь] уже администратор."
        ALLOWED_IDS.add(user_id)
        if user_id not in data["admins"]:
            data["admins"].append(user_id)
        save_data()
        return "[id" + str(user_id) + "|Пользователь] теперь администратор."
def cmd_revoke_admin(args):
    if len(args) != 1:
        return "Формат: /снятьвд @username"
    user_id = resolve_user(args[0])
    if user_id is None:
        return "Пользователь " + args[0] + " не найден в VK."
    if user_id in BASE_ADMIN_IDS:
        return "Этого админа нельзя снять командой, он прописан в коде."
    with lock:
        if user_id not in ALLOWED_IDS:
            return "[id" + str(user_id) + "|Пользователь] не администратор."
        ALLOWED_IDS.discard(user_id)
        if user_id in data["admins"]:
            data["admins"].remove(user_id)
        save_data()
        return "У [id" + str(user_id) + "|пользователя] забран доступ админа."
# ==========================================================
# СПИСОК — баллы (или отпуск/отгул) и ссылка на ВК
# ==========================================================
def cmd_list(args):
    if args:
        return "Формат: /список"
    with lock:
        players = sorted(
            list(data["players"].values()),
            key=lambda p: (-p["points"], p["nick"].lower()),
        )
        if not players:
            return "Список пуст."
        lines = []
        for i, p in enumerate(players, 1):
            vacation = p.get("vacation")
            otgul = p.get("otgul", 0)
            link = "(vk.com/id" + str(p["vk_id"]) + ")"
            if vacation:
                status = "отпуск до " + vacation
            elif otgul:
                status = "отгул"
            else:
                status = str(p["points"])
            lines.append(str(i) + ". " + p["nick"] + " — " + status + " " + link)
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
                print("Рассылка: не удалось отправить " + str(uid) + ":", repr(e))
            time.sleep(0.1)
        return "Рассылка завершена. Отправлено: " + str(sent) + ", не доставлено: " + str(failed) + "."
# ==========================================================
# БАЛЛЫ ТЕКУЩЕГО ПОЛЬЗОВАТЕЛЯ
# ==========================================================
def cmd_points(user_id):
    with lock:
        player = find_by_id(user_id)
        if player is None:
            return "Вы не привязаны к нику. Обратитесь к администратору."
        return player["nick"] + ", у вас " + str(player["points"]) + " баллов."
# ==========================================================
# МАГАЗИН
# ==========================================================
def cmd_shop():
    with lock:
        shop = list(data["shop"])
        if not shop:
            return "Магазин пуст."
        lines = [
            str(i) + ". " + item["name"] + " — " + str(item["price"]) + " баллов"
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
            return "Не хватает баллов. Нужно " + str(item["price"]) + ", у вас " + str(player["points"]) + "."
        item_name = item["name"]
        item_price = item["price"]
        name_lower = item_name.lower().strip()
        nick = player["nick"]

        player["points"] -= item_price
        left = player["points"]

        if name_lower == "отгул":
            player["otgul"] = player.get("otgul", 0) + 1
            answer = "Вы купили отгул! Осталось баллов: " + str(left) + "."
        elif name_lower == "отпуск":
            player["waiting_vacation"] = True
            answer = "Ваш отпуск оплачен! Напишите дату окончания в формате ДД.ММ (например 15.12):"
        else:
            answer = "Вы купили: " + item_name + ". Осталось баллов: " + str(left) + "."

        save_data()
        # Уведомление админам при ЛЮБОЙ покупке (включая отгул и отпуск)
        executor.submit(
            notify_admins,
            "[id" + str(user_id) + "|@" + nick + "] (" + nick + ") купил №" + str(num) + ": "
            + item_name + " (" + str(item_price) + " баллов). Выдайте ему.",
        )
        return answer
def cmd_shop_add(args):
    if len(args) < 2 or not re.fullmatch(r"\d+", args[0]):
        return "Формат: /+магазин 100 что выдать"
    price = int(args[0])
    if price <= 0:
        return "Цена должна быть больше 0."
    name = " ".join(args[1:])
    with lock:
        data["shop"].append({"name": name, "price": price})
        num = len(data["shop"])
        save_data()
        return "Товар добавлен под номером " + str(num) + ": " + name + " — " + str(price) + " баллов."
def cmd_shop_remove(args):
    if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
        return "Формат: /-магазин 3"
    num = int(args[0])
    with lock:
        if num < 1 or num > len(data["shop"]):
            return "Товара с таким номером нет."
        item = data["shop"].pop(num - 1)
        item_name = item["name"]
        save_data()
        return "Товар удалён: " + item_name + "."
# ==========================================================
# HELP
# ==========================================================
USER_HELP = (
    "Ваши команды:\n"
    "/баллы\n"
    "/магазин\n"
    "/купить\n"
    "/help — список команд"
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
        print("Сообщение от " + str(from_id) + ": " + repr(text))
    # === Ожидание даты отпуска: игрок пишет просто дату без команды ===
    if not text.startswith("/"):
        with lock:
            player = find_by_id(from_id)
            if player and player.get("waiting_vacation"):
                date = text.strip()
                if re.fullmatch(r"\d{1,2}\.\d{1,2}(\.\d{2,4})?", date):
                    player["vacation"] = date
                    player["waiting_vacation"] = False
                    save_data()
                    reply(peer_id, "Отпуск зафиксирован до " + date + ".")
                else:
                    reply(peer_id, "Напишите дату в формате ДД.ММ (например 15.12):")
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
            public_answer = "Ваш VK ID: " + str(from_id)
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
    print(
        "GIST_ENABLED:", GIST_ENABLED,
        "| GIST_ID:", (GIST_ID[:6] + "...") if GIST_ID else "(пусто)",
        "| длина токена:", len(GIST_TOKEN),
    )
    if ON_GITHUB_ACTIONS and not GIST_ENABLED:
        raise SystemExit(
            "На GitHub Actions локальный data.json исчезает после перезапуска. "
            "Добавьте GIST_TOKEN и GIST_ID в Secrets репозитория."
        )
    if GIST_ENABLED:
        print("Постоянное хранилище: GitHub Gist включено.")
    else:
        print("ВНИМАНИЕ: GIST_TOKEN/GIST_ID не заданы — данные хранятся только локально!")
    load_data()
    print(
        "Игроков: " + str(len(data["players"])) + ", "
        + "товаров: " + str(len(data["shop"])) + ", админов: " + str(len(ALLOWED_IDS))
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
