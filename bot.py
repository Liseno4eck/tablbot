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
# Общие админы: имеют доступ ко ВСЕМ спискам (/список и /нлист)
ALLOWED_IDS = {
    875762552,
    857698829,
    708897961
}
BASE_ADMIN_IDS = set(ALLOWED_IDS)
# Админы второго списка (/нлист): выдаются командой /вдв
NADMIN_IDS = set()
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
# Список 1 (/список):  players, shop, admins
# Список 2 (/нлист):   nplayers, nshop, nadmins
# players: nick_lower -> {nick, vk_id, points, vacation, otgul, waiting_vacation}
# ==========================================================
lock = threading.RLock()
save_lock = threading.Lock()
data = {
    "players": {}, "shop": [], "admins": [],
    "nplayers": {}, "nshop": [], "nadmins": [],
}
LIST_TITLE = {1: "/список", 2: "/нлист"}
def P(n):
    return data["players"] if n == 1 else data["nplayers"]
def S(n):
    return data["shop"] if n == 1 else data["nshop"]
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
DATA_KEYS = ("players", "shop", "admins", "nplayers", "nshop", "nadmins")
def _has_content(d):
    return bool(d) and any(d.get(key) for key in DATA_KEYS)
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
def _norm_players(raw_players):
    players = {}
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
            try:
                otgul = int(value.get("otgul", 0) or 0)
            except (TypeError, ValueError):
                otgul = 0
            if not nick or vk_id <= 0:
                continue
            players[nick.lower()] = {
                "nick": nick,
                "vk_id": vk_id,
                "points": points,
                "vacation": value.get("vacation"),
                "otgul": otgul,
                "waiting_vacation": bool(value.get("waiting_vacation", False)),
            }
    return players
def _norm_shop(raw_shop):
    shop = []
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
    return shop
def _norm_ids(raw_ids):
    ids = []
    if isinstance(raw_ids, list):
        for value in raw_ids:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                pass
    return ids
def _normalize_loaded(loaded):
    return {
        "players": _norm_players(loaded.get("players")),
        "shop": _norm_shop(loaded.get("shop")),
        "admins": _norm_ids(loaded.get("admins")),
        "nplayers": _norm_players(loaded.get("nplayers")),
        "nshop": _norm_shop(loaded.get("nshop")),
        "nadmins": _norm_ids(loaded.get("nadmins")),
    }
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
    for key in DATA_KEYS:
        data[key] = normalized[key]
    ALLOWED_IDS.clear()
    ALLOWED_IDS.update(BASE_ADMIN_IDS)
    ALLOWED_IDS.update(data["admins"])
    NADMIN_IDS.clear()
    NADMIN_IDS.update(data["nadmins"])
    if need_push:
        if not save_data():
            raise SystemExit("Не удалось записать начальные данные в Gist. Бот остановлен.")
    elif GIST_ENABLED:
        with lock:
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            _save_local(payload)
def find_by_nick(n, nick):
    return P(n).get(nick.lower())
def find_by_id(n, vk_id):
    for p in P(n).values():
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
    """Уведомление общим админам (покупки из списка 1)."""
    for admin_id in list(ALLOWED_IDS):
        try:
            vk.messages.send(user_id=admin_id, message=text, random_id=get_random_id())
        except Exception as e:
            print("Не удалось написать " + str(admin_id) + ":", repr(e))
def notify_nadmins(text):
    """Уведомление ТОЛЬКО тем, кто получил доступ через /вдв (покупки из /нлист)."""
    ids = list(NADMIN_IDS)
    if not ids:
        print("Покупка в /нлист, но нет ни одного админа с /вдв: " + text)
        return
    for admin_id in ids:
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
def cmd_give(args, sign, n):
    if len(args) != 2 or not re.fullmatch(r"\d+", args[1]):
        command = "/выдать" if sign > 0 else "/забрать"
        return "Формат: " + command + " Nick_Name 5"
    amount = int(args[1])
    if amount <= 0:
        return "Количество баллов должно быть больше 0."
    with lock:
        player = find_by_nick(n, args[0])
        if player is None:
            return "Игрок " + args[0] + " не найден (" + LIST_TITLE[n] + ")."
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
def cmd_bind(args, n):
    if len(args) != 2:
        return "Формат: /привязать @username Nick_Name"
    username, nick = args
    user_id = resolve_user(username)
    if user_id is None:
        return "Пользователь " + username + " не найден в VK."
    with lock:
        if find_by_nick(n, nick) is not None:
            return "Ник " + nick + " уже занят (" + LIST_TITLE[n] + ")."
        existing = find_by_id(n, user_id)
        if existing is not None:
            return "Этот пользователь уже привязан к нику " + existing["nick"] + " (" + LIST_TITLE[n] + ")."
        P(n)[nick.lower()] = {
            "nick": nick,
            "vk_id": user_id,
            "points": 0,
            "vacation": None,
            "otgul": 0,
            "waiting_vacation": False,
        }
        save_data()
        return "Игрок " + nick + " привязан к https://vk.com/id" + str(user_id) + " (" + LIST_TITLE[n] + ")."
# ==========================================================
# ОТВЯЗАТЬ
# ==========================================================
def cmd_unbind(args, n):
    if len(args) != 1:
        return "Формат: /отвязать @username или /отвязать Nick_Name"
    target = args[0]
    with lock:
        player = find_by_nick(n, target)
        if player is not None:
            nick = player["nick"]
            del P(n)[nick.lower()]
            save_data()
            return "Игрок " + nick + " отвязан (" + LIST_TITLE[n] + ")."
    user_id = resolve_user(target)
    if user_id is None:
        return "Игрок " + target + " не найден."
    with lock:
        player = find_by_id(n, user_id)
        if player is None:
            return "Игрок " + target + " не найден (" + LIST_TITLE[n] + ")."
        nick = player["nick"]
        del P(n)[nick.lower()]
        save_data()
        return "Игрок " + nick + " отвязан (" + LIST_TITLE[n] + ")."
# ==========================================================
# ИЗМЕНИТЬ (смена ника)
# ==========================================================
def cmd_change(args, n):
    if len(args) != 2:
        return "Формат: /изменить Старый_Ник Новый_Ник"
    old, new = args
    with lock:
        player = find_by_nick(n, old)
        if player is None:
            return "Игрок " + old + " не найден (" + LIST_TITLE[n] + ")."
        if old.lower() != new.lower() and find_by_nick(n, new) is not None:
            return "Ник " + new + " уже занят."
        old_nick = player["nick"]
        del P(n)[old_nick.lower()]
        player["nick"] = new
        P(n)[new.lower()] = player
        save_data()
        return "Ник изменён: " + old_nick + " -> " + new + "."
# ==========================================================
# ВД — полный доступ общего админа
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
# ВДВ — доступ к боту только для списка /нлист
# ==========================================================
def cmd_grant_nadmin(args):
    if len(args) != 1:
        return "Формат: /вдв @username"
    user_id = resolve_user(args[0])
    if user_id is None:
        return "Пользователь " + args[0] + " не найден в VK."
    with lock:
        if user_id in NADMIN_IDS:
            return "[id" + str(user_id) + "|Пользователь] уже имеет доступ к /нлист."
        NADMIN_IDS.add(user_id)
        if user_id not in data["nadmins"]:
            data["nadmins"].append(user_id)
        save_data()
        return "[id" + str(user_id) + "|Пользователь] теперь имеет доступ к боту для списка /нлист."
def cmd_revoke_nadmin(args):
    if len(args) != 1:
        return "Формат: /снятьвдв @username"
    user_id = resolve_user(args[0])
    if user_id is None:
        return "Пользователь " + args[0] + " не найден в VK."
    with lock:
        if user_id not in NADMIN_IDS:
            return "[id" + str(user_id) + "|Пользователь] не имеет доступа к /нлист."
        NADMIN_IDS.discard(user_id)
        if user_id in data["nadmins"]:
            data["nadmins"].remove(user_id)
        save_data()
        return "У [id" + str(user_id) + "|пользователя] забран доступ к /нлист."
# ==========================================================
# СПИСОК / НЛИСТ — баллы (или отпуск/отгул) и ссылка на ВК
# ==========================================================
def cmd_list(args, n):
    if args:
        return "Формат: " + LIST_TITLE[n]
    with lock:
        players = sorted(
            list(P(n).values()),
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
        title = "Участники:" if n == 1 else "Участники (нлист):"
        return title + "\n" + "\n".join(lines)
# ==========================================================
# РАЗОСЛАТЬ
# ==========================================================
def cmd_broadcast(raw_text, n):
    parts = raw_text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return "Формат: /разослать текст"
    message = parts[1].strip()
    with lock:
        ids = list(dict.fromkeys(p["vk_id"] for p in P(n).values()))
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
    return "Рассылка завершена (" + LIST_TITLE[n] + "). Отправлено: " + str(sent) + ", не доставлено: " + str(failed) + "."
# ==========================================================
# БАЛЛЫ ТЕКУЩЕГО ПОЛЬЗОВАТЕЛЯ
# ==========================================================
def cmd_points(user_id, n):
    with lock:
        player = find_by_id(n, user_id)
        if player is None:
            return "Вы не привязаны к нику. Обратитесь к администратору."
        return player["nick"] + ", у вас " + str(player["points"]) + " баллов."
# ==========================================================
# МАГАЗИН
# ==========================================================
def cmd_shop(n):
    with lock:
        shop = list(S(n))
        if not shop:
            return "Магазин пуст."
        lines = [
            str(i) + ". " + item["name"] + " — " + str(item["price"]) + " баллов"
            for i, item in enumerate(shop, 1)
        ]
        return "Магазин:\n" + "\n".join(lines) + "\n\nКупить: /купить номер"
def cmd_buy(args, user_id, n):
    if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
        return "Формат: /купить 1"
    num = int(args[0])
    with lock:
        player = find_by_id(n, user_id)
        if player is None:
            return "Вы не привязаны к нику. Обратитесь к администратору."
        shop = S(n)
        if num < 1 or num > len(shop):
            return "Товара с таким номером нет."
        item = shop[num - 1]
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
        # Уведомление при ЛЮБОЙ покупке (включая отгул и отпуск):
        # список 1 -> общим админам, /нлист -> только тем, кто получил /вдв
        text = (
            "[id" + str(user_id) + "|@" + nick + "] (" + nick + ") купил №" + str(num) + ": "
            + item_name + " (" + str(item_price) + " баллов). Выдайте ему."
        )
        if n == 1:
            executor.submit(notify_admins, text)
        else:
            executor.submit(notify_nadmins, "[/нлист] " + text)
        return answer
def cmd_shop_add(args, n):
    if len(args) < 2 or not re.fullmatch(r"\d+", args[0]):
        return "Формат: /+магазин 100 что выдать"
    price = int(args[0])
    if price <= 0:
        return "Цена должна быть больше 0."
    name = " ".join(args[1:])
    with lock:
        S(n).append({"name": name, "price": price})
        num = len(S(n))
        save_data()
        return "Товар добавлен (" + LIST_TITLE[n] + ") под номером " + str(num) + ": " + name + " — " + str(price) + " баллов."
def cmd_shop_remove(args, n):
    if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
        return "Формат: /-магазин 3"
    num = int(args[0])
    with lock:
        shop = S(n)
        if num < 1 or num > len(shop):
            return "Товара с таким номером нет."
        item = shop.pop(num - 1)
        item_name = item["name"]
        save_data()
        return "Товар удалён (" + LIST_TITLE[n] + "): " + item_name + "."
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
    "Для администраторов (два независимых списка: /список и /нлист):\n"
    "/список — список участников с баллами\n"
    "/нлист — второй список участников\n"
    "/привязать1 @username Nick_Name — привязать в /список\n"
    "/привязать2 @username Nick_Name — привязать в /нлист\n"
    "/+магазин1 100 товар — добавить товар в магазин /список\n"
    "/+магазин2 100 товар — добавить товар в магазин /нлист\n"
    "/-магазин1 3 и /-магазин2 3 — удалить товар по номеру\n"
    "/разослать1 текст и /разослать2 текст — рассылка по списку\n"
    "/выдать Nick_Name 5 — выдать баллы\n"
    "/забрать Nick_Name 5 — забрать баллы\n"
    "/отвязать @username или Nick_Name — отвязать игрока\n"
    "/изменить Старый Новый — сменить ник\n"
    "(выдать/забрать/отвязать/изменить сами находят список по нику; "
    "если ник есть в обоих — добавьте 1 или 2: /выдать2 Nick 5)\n"
    "/вд @username — выдать полный доступ админа\n"
    "/снятьвд @username — забрать доступ админа\n"
    "/вдв @username — выдать доступ только к списку /нлист\n"
    "/снятьвдв @username — забрать доступ к /нлист"
)
NADMIN_HELP = (
    "Все команды:\n\n"
    "Для всех:\n"
    "/баллы — узнать свои баллы\n"
    "/магазин — список товаров\n"
    "/купить (номер) — купить товар\n"
    "/help — список команд\n\n"
    "Для администратора /нлист:\n"
    "/нлист — список участников с баллами\n"
    "/выдать Nick_Name 5 — выдать баллы\n"
    "/забрать Nick_Name 5 — забрать баллы\n"
    "/привязать @username Nick_Name — привязать игрока\n"
    "/отвязать @username или Nick_Name — отвязать игрока\n"
    "/изменить Старый Новый — сменить ник\n"
    "/разослать текст — рассылка привязанным игрокам\n"
    "/+магазин 100 товар — добавить товар в магазин\n"
    "/-магазин 3 — удалить товар из магазина по номеру"
)
def cmd_help(user_id):
    if user_id in ALLOWED_IDS:
        return ADMIN_HELP
    if user_id in NADMIN_IDS:
        return NADMIN_HELP
    return USER_HELP
# ==========================================================
# РАЗБОР КОМАНД С НОМЕРОМ СПИСКА (/привязать1, /привязать2 ...)
# ==========================================================
LIST_COMMANDS = {
    "/выдать", "/забрать", "/привязать", "/отвязать", "/убрать",
    "/изменить", "/+магазин", "/-магазин", "/разослать",
    "/баллы", "/магазин", "/купить",
}
# Эти команды общий админ ОБЯЗАН вводить с номером списка
NEED_SUFFIX = {"/привязать", "/+магазин", "/-магазин", "/разослать"}
def split_command(cmd):
    """'/привязать2' -> ('/привязать', 2); '/привязать' -> ('/привязать', None)"""
    if len(cmd) > 1 and cmd[-1] in "12" and cmd[:-1] in LIST_COMMANDS:
        return cmd[:-1], int(cmd[-1])
    return cmd, None
def ask_suffix(command):
    return (
        "Укажите, для какого списка:\n"
        + command + "1 — для /список\n"
        + command + "2 — для /нлист"
    )
def auto_ctx(command, args):
    """Для общего админа без номера: определить список по нику (или id).
    Возвращает 1/2 либо строку с просьбой уточнить."""
    if not args:
        return 1
    target = args[0]
    with lock:
        found = [n for n in (1, 2) if find_by_nick(n, target) is not None]
    if not found and command in ("/отвязать", "/убрать"):
        user_id = resolve_user(target)
        if user_id is not None:
            with lock:
                found = [n for n in (1, 2) if find_by_id(n, user_id) is not None]
    if len(found) == 2:
        return (
            "«" + target + "» есть в обоих списках. Уточните:\n"
            + command + "1 — /список\n"
            + command + "2 — /нлист"
        )
    if len(found) == 1:
        return found[0]
    return 1  # не найден нигде — команда сама ответит «не найден»
# ==========================================================
# ОБРАБОТКА АДМИНСКИХ КОМАНД
# ==========================================================
def handle(text, user_id):
    parts = text.strip().split()
    if not parts:
        return None
    raw_cmd = parts[0].lower()
    args = parts[1:]
    is_common = user_id in ALLOWED_IDS
    is_nadmin = user_id in NADMIN_IDS

    # Просмотр списков
    if raw_cmd in ("/нлист", "/nlist"):
        return cmd_list(args, 2)
    if raw_cmd == "/список":
        return cmd_list(args, 1) if is_common else None

    # Управление доступом — только общие админы
    if raw_cmd == "/вд":
        return cmd_grant_admin(args) if is_common else None
    if raw_cmd == "/снятьвд":
        return cmd_revoke_admin(args) if is_common else None
    if raw_cmd == "/вдв":
        return cmd_grant_nadmin(args) if is_common else None
    if raw_cmd == "/снятьвдв":
        return cmd_revoke_nadmin(args) if is_common else None

    command, suffix = split_command(raw_cmd)
    if command not in LIST_COMMANDS or command in ("/баллы", "/магазин", "/купить"):
        return None

    # Определяем, с каким списком работаем
    if is_common:
        n = suffix
        if n is None:
            if command in NEED_SUFFIX:
                return ask_suffix(command)
            n = auto_ctx(command, args)
            if isinstance(n, str):
                return n
    elif is_nadmin:
        if suffix == 1:
            return None
        n = 2
    else:
        return None

    if command == "/выдать":
        return cmd_give(args, +1, n)
    if command == "/забрать":
        return cmd_give(args, -1, n)
    if command == "/привязать":
        return cmd_bind(args, n)
    if command in ("/отвязать", "/убрать"):
        return cmd_unbind(args, n)
    if command == "/изменить":
        return cmd_change(args, n)
    if command == "/+магазин":
        return cmd_shop_add(args, n)
    if command == "/-магазин":
        return cmd_shop_remove(args, n)
    if command == "/разослать":
        return cmd_broadcast(text, n)
    return None
# ==========================================================
# СПИСОК ПОЛЬЗОВАТЕЛЯ ДЛЯ ПУБЛИЧНЫХ КОМАНД
# ==========================================================
def public_ctx(user_id, suffix):
    if suffix in (1, 2):
        return suffix
    with lock:
        in1 = find_by_id(1, user_id) is not None
        in2 = find_by_id(2, user_id) is not None
    if in2 and not in1:
        return 2
    return 1
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
            for n in (1, 2):
                player = find_by_id(n, from_id)
                if player and player.get("waiting_vacation"):
                    date = text.strip()
                    if re.fullmatch(r"\d{1,2}\.\d{1,2}(\.\d{2,4})?", date):
                        player["vacation"] = date
                        player["waiting_vacation"] = False
                        save_data()
                        reply(peer_id, "Отпуск зафиксирован до " + date + ".")
                    else:
                        reply(peer_id, "Напишите дату в формате ДД.ММ (например 15.12):")
                    break
        return
    # ======================================================
    # ПУБЛИЧНЫЕ КОМАНДЫ (доступны всем)
    # ======================================================
    parts = text.split()
    raw_cmd = parts[0].lower()
    args = parts[1:]
    command, suffix = split_command(raw_cmd)
    is_public = True
    public_answer = None
    try:
        if command == "/баллы":
            n = public_ctx(from_id, suffix)
            public_answer = "Формат: /баллы" if args else cmd_points(from_id, n)
        elif raw_cmd in ("/help", "/хелп"):
            public_answer = cmd_help(from_id)
        elif command == "/магазин":
            public_answer = cmd_shop(public_ctx(from_id, suffix))
        elif command == "/купить":
            public_answer = cmd_buy(args, from_id, public_ctx(from_id, suffix))
        elif raw_cmd == "/я":
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
    # ВСЕ ОСТАЛЬНЫЕ КОМАНДЫ — только админы (общие или /вдв)
    # ======================================================
    if from_id not in ALLOWED_IDS and from_id not in NADMIN_IDS:
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
        "Список 1: игроков " + str(len(data["players"])) + ", товаров " + str(len(data["shop"]))
        + " | /нлист: игроков " + str(len(data["nplayers"])) + ", товаров " + str(len(data["nshop"]))
        + " | общих админов: " + str(len(ALLOWED_IDS)) + ", админов /нлист: " + str(len(NADMIN_IDS))
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
