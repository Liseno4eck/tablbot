import os
import re
import json
import time
import zlib
import threading
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone, date
import vk_api
from concurrent.futures import ThreadPoolExecutor
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
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
# Владелец: ТОЛЬКО ему приходят обращения из /написать.
# Поменяйте на свой VK ID (или задайте переменную окружения OWNER_ID)
OWNER_ID = int(os.environ.get("OWNER_ID", "875762552"))
DATA_FILE = os.environ.get("DATA_FILE", "data.json")
GIST_TOKEN_FALLBACK = ""
GIST_ID_FALLBACK = ""
# Всё время (даты отпуска) считается по Москве
MSK = timezone(timedelta(hours=3))
DEFAULT_VACATION_MAX_DAYS = 4
# Сколько мест показывать в /топ
TOP_LIMIT = 10
# Сколько товаров на одной странице кнопок магазина
SHOP_PER_PAGE = 6
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
# contacts: все, кто писал боту в ЛС (для /рассылка)
# players: nick_lower -> {
#   nick, vk_id, points, vacation (строка для показа), otgul,
#   vac_stage (None | "date" | "incl"),
#   vac_max (макс. дней), vac_pending (данные покупки до подтверждения),
#   vac_end (ISO дата окончания), vac_incl (True/False/None),
#   purchases (история покупок для /откат)
# }
# ==========================================================
lock = threading.RLock()
save_lock = threading.Lock()
data = {
    "players": {}, "shop": [], "admins": [],
    "nplayers": {}, "nshop": [], "nadmins": [],
    "contacts": [],
}
CONTACTS = set()
LAST_BUY = {}
LIST_TITLE = {1: "/список", 2: "/нлист"}
def P(n):
    return data["players"] if n == 1 else data["nplayers"]
def S(n):
    return data["shop"] if n == 1 else data["nshop"]
def today_local():
    """Сегодняшняя дата по Москве."""
    return datetime.now(MSK).date()
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
DATA_KEYS = ("players", "shop", "admins", "nplayers", "nshop", "nadmins", "contacts")
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
def _norm_purchases(raw):
    result = []
    if isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict):
                continue
            try:
                price = int(it.get("price", 0))
                num = int(it.get("num", 0))
            except (TypeError, ValueError):
                continue
            kind = it.get("kind")
            if kind not in ("vacation", "otgul", "other"):
                kind = "other"
            result.append({
                "item": str(it.get("item") or ""),
                "price": price,
                "num": num,
                "kind": kind,
                "ts": str(it.get("ts") or ""),
            })
    return result[-30:]
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
            # стадия ожидания отпуска (с совместимостью со старым waiting_vacation)
            vac_stage = value.get("vac_stage")
            if vac_stage not in ("date", "incl"):
                vac_stage = None
            vac_pending = value.get("vac_pending")
            if not isinstance(vac_pending, dict):
                vac_pending = None
            if value.get("waiting_vacation") and vac_stage is None:
                vac_stage = "date"
                vac_pending = vac_pending or {"num": 0, "item": "отпуск", "price": 0}
            try:
                vac_max = int(value.get("vac_max", DEFAULT_VACATION_MAX_DAYS))
            except (TypeError, ValueError):
                vac_max = DEFAULT_VACATION_MAX_DAYS
            vac_incl = value.get("vac_incl")
            if vac_incl not in (True, False):
                vac_incl = None
            players[nick.lower()] = {
                "nick": nick,
                "vk_id": vk_id,
                "points": points,
                "vacation": value.get("vacation"),
                "otgul": otgul,
                "vac_stage": vac_stage,
                "vac_max": vac_max,
                "vac_pending": vac_pending,
                "vac_end": value.get("vac_end"),
                "vac_incl": vac_incl,
                "purchases": _norm_purchases(value.get("purchases")),
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
        "contacts": list(dict.fromkeys(_norm_ids(loaded.get("contacts")))),
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
    CONTACTS.clear()
    CONTACTS.update(data["contacts"])
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
def register_contact(user_id):
    """Запоминает того, кто написал боту в ЛС. True — если это новый пользователь."""
    if user_id <= 0:
        return False
    with lock:
        if user_id in CONTACTS:
            return False
        CONTACTS.add(user_id)
        data["contacts"].append(user_id)
        save_data()
    return True
# ==========================================================
# ОТПУСК: даты и актуальность (время по МСК)
# ==========================================================
DATE_RE = r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?"
def parse_vacation_date(text):
    """'15.12' или '15.12.2026' -> date. Без года: ближайшая дата, не раньше сегодня (МСК)."""
    match = re.fullmatch(DATE_RE, text.strip())
    if not match:
        return None
    day = int(match.group(1))
    month = int(match.group(2))
    year_raw = match.group(3)
    today = today_local()
    try:
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
            return date(year, month, day)
        result = date(today.year, month, day)
        if result < today:
            result = date(today.year + 1, month, day)
        return result
    except ValueError:
        return None
def format_date(d):
    if d.year == today_local().year:
        return d.strftime("%d.%m")
    return d.strftime("%d.%m.%Y")
def vacation_max_days_from_name(name):
    match = re.search(r"до\s*(\d+)\s*дн", name.lower())
    if match:
        return int(match.group(1))
    return DEFAULT_VACATION_MAX_DAYS
def is_vacation_item(name):
    return name.lower().strip().startswith("отпуск")
def _clear_vacation(player):
    player["vacation"] = None
    player["vac_end"] = None
    player["vac_incl"] = None
def vacation_active(player):
    """Возвращает (active, changed). Просроченный отпуск сбрасывается.
    Не включительно: в дату окончания уже показываются баллы.
    Включительно: дата окончания ещё отпуск, со следующего дня баллы."""
    vacation = player.get("vacation")
    if not vacation:
        return False, False
    end = None
    end_iso = player.get("vac_end")
    if end_iso:
        try:
            end = date.fromisoformat(end_iso)
        except ValueError:
            end = None
    incl = player.get("vac_incl")
    if end is None:
        # старые записи без точной даты: пробуем разобрать строку
        match = re.fullmatch(DATE_RE, str(vacation).strip())
        if not match:
            return True, False
        try:
            year_raw = match.group(3)
            if year_raw:
                year = int(year_raw)
                if year < 100:
                    year += 2000
            else:
                year = today_local().year
            end = date(year, int(match.group(2)), int(match.group(1)))
        except ValueError:
            return True, False
        incl = True
    if incl is None:
        incl = True
    today = today_local()
    expired = (today > end) if incl else (today >= end)
    if expired:
        _clear_vacation(player)
        return False, True
    return True, False
# ==========================================================
# ОТПРАВКА СООБЩЕНИЙ
# ==========================================================
def reply(peer_id, text, keyboard=None):
    try:
        params = {"peer_id": peer_id, "message": str(text), "random_id": get_random_id()}
        if keyboard:
            params["keyboard"] = keyboard
        vk.messages.send(**params)
    except Exception as e:
        print("Ошибка отправки сообщения:", repr(e))
def send_to_user(user_id, text):
    try:
        vk.messages.send(user_id=user_id, message=text, random_id=get_random_id())
    except Exception as e:
        print("Не удалось написать " + str(user_id) + ":", repr(e))
def notify_admins(text):
    """Уведомление общим админам (покупки из списка 1)."""
    for admin_id in list(ALLOWED_IDS):
        send_to_user(admin_id, text)
def notify_nadmins(text):
    """Уведомление ТОЛЬКО тем, кто получил доступ через /вдв (покупки из /нлист)."""
    ids = list(NADMIN_IDS)
    if not ids:
        print("Покупка в /нлист, но нет ни одного админа с /вдв: " + text)
        return
    for admin_id in ids:
        send_to_user(admin_id, text)
def notify_for_list(n, text):
    if n == 1:
        executor.submit(notify_admins, text)
    else:
        executor.submit(notify_nadmins, "[/нлист] " + text)
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
def get_user_name(user_id):
    try:
        users = vk.users.get(user_ids=user_id)
        if users:
            name = (users[0].get("first_name", "") + " " + users[0].get("last_name", "")).strip()
            name = re.sub(r"[\[\]|]", "", name)
            if name:
                return name
    except Exception as e:
        print("Ошибка получения имени:", repr(e))
    return "пользователя"
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
def new_player(nick, user_id):
    return {
        "nick": nick,
        "vk_id": user_id,
        "points": 0,
        "vacation": None,
        "otgul": 0,
        "vac_stage": None,
        "vac_max": DEFAULT_VACATION_MAX_DAYS,
        "vac_pending": None,
        "vac_end": None,
        "vac_incl": None,
        "purchases": [],
    }
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
        P(n)[nick.lower()] = new_player(nick, user_id)
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
# ОТКАТ последней покупки (/откат или /октат)
# ==========================================================
RETURN_WORDS = {"возврат", "возвращ", "возвратить", "вернуть"}
def cmd_rollback(args, n):
    usage = "Формат: /откат Nick_Name или @username\n/откат Nick_Name возврат — вернуть и баллы"
    if not args:
        return usage
    target = args[0]
    rest = [a.lower() for a in args[1:]]
    refund = any(a in RETURN_WORDS for a in rest)
    if any(a not in RETURN_WORDS for a in rest):
        return usage

    with lock:
        player = find_by_nick(n, target)
    if player is None:
        user_id = resolve_user(target)
        if user_id is None:
            return "Игрок " + target + " не найден."
        with lock:
            player = find_by_id(n, user_id)
        if player is None:
            return "Игрок " + target + " не найден (" + LIST_TITLE[n] + ")."

    with lock:
        purchases = player.get("purchases") or []
        if not purchases:
            return "У " + player["nick"] + " нет покупок для отката (" + LIST_TITLE[n] + ")."
        last = purchases.pop()
        player["purchases"] = purchases
        kind = last.get("kind")
        if kind == "vacation":
            _clear_vacation(player)
            player["vac_stage"] = None
            player["vac_pending"] = None
        elif kind == "otgul":
            player["otgul"] = max(0, int(player.get("otgul", 0) or 0) - 1)
        price = int(last.get("price", 0))
        if refund:
            player["points"] += price
        nick = player["nick"]
        vk_id = player["vk_id"]
        points = player["points"]
        item_name = last.get("item") or "покупка"
        save_data()

    if refund:
        user_text = "Ваша покупка «" + item_name + "» отменена администратором. Баллы возвращены: +" + str(price) + "."
        admin_text = (
            "Покупка «" + item_name + "» у " + nick + " отменена, баллы возвращены (+" + str(price)
            + "). Сейчас у него " + str(points) + " баллов."
        )
    else:
        user_text = "Ваша покупка «" + item_name + "» отменена администратором. Баллы не возвращены."
        admin_text = (
            "Покупка «" + item_name + "» у " + nick + " отменена, баллы не возвращены. Сейчас у него "
            + str(points) + " баллов."
        )
    executor.submit(send_to_user, vk_id, user_text)
    return admin_text
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
        changed = False
        for p in P(n).values():
            _, ch = vacation_active(p)
            changed = changed or ch
        if changed:
            save_data()
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
                status = "отпуск до " + str(vacation)
            elif otgul:
                status = "отгул"
            else:
                status = str(p["points"])
            lines.append(str(i) + ". " + p["nick"] + " — " + status + " " + link)
        title = "Участники:" if n == 1 else "Участники (нлист):"
        return title + "\n" + "\n".join(lines)
# ==========================================================
# ТОП ПО БАЛЛАМ
# ==========================================================
def _format_top(players, title):
    players = sorted(players, key=lambda p: (-p["points"], p["nick"].lower()))
    if not players:
        return title + "\nСписок пуст."
    lines = []
    for i, p in enumerate(players[:TOP_LIMIT], 1):
        lines.append(str(i) + ". " + p["nick"] + " — " + str(p["points"]))
    return title + "\n" + "\n".join(lines)
def cmd_top(n):
    """Топ по баллам одного списка (n = 1 или 2)."""
    with lock:
        players = list(P(n).values())
    if n == 1:
        title = "🏆 Топ по баллам (/список):"
    else:
        title = "🏆 Топ по баллам (/нлист):"
    return _format_top(players, title)
def cmd_top_all():
    """Общий топ по баллам из обоих списков."""
    with lock:
        players = list(P(1).values()) + list(P(2).values())
    return _format_top(players, "🌐 Общий топ по баллам:")
def top_lists(user_id):
    """Какие топы доступны пользователю (список номеров 1/2)."""
    with lock:
        b1 = find_by_id(1, user_id) is not None
        b2 = find_by_id(2, user_id) is not None
    if user_id in ALLOWED_IDS:
        if b1 and not b2:
            return [1]
        if b2 and not b1:
            return [2]
        return [1, 2]
    result = set()
    if user_id in NADMIN_IDS:
        result.add(2)
    if b1:
        result.add(1)
    if b2:
        result.add(2)
    return sorted(result)
def handle_top(user_id, suffix, args):
    """Текстовые /топ, /топ1, /топ2, /топ общий (остались для беседы и для тех, кто пишет руками).
    Доступ: все, кто привязан хотя бы в одном списке, и админы."""
    lists = top_lists(user_id)
    if not lists:
        return "Вы не привязаны к нику. Обратитесь к администратору."
    if args and args[0].lower() in ("общий", "общ"):
        return cmd_top_all()
    if args:
        return "Формат: /топ или /топ общий"
    if suffix in (1, 2):
        if suffix in lists or user_id in ALLOWED_IDS:
            return cmd_top(suffix)
        return "Нет доступа к этому списку."
    if len(lists) == 1:
        return cmd_top(lists[0])
    return (
        "Укажите, для какого списка:\n"
        "/топ1 — для /список\n"
        "/топ2 — для /нлист\n"
        "/топ общий — общий топ"
    )
# ==========================================================
# РАЗОСЛАТЬ (по одному списку) и РАССЫЛКА (всем, кто писал боту)
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
def cmd_broadcast_all(raw_text):
    parts = raw_text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return "Формат: /рассылка текст"
    message = parts[1].strip()
    with lock:
        ids = set(CONTACTS)
        for n in (1, 2):
            for p in P(n).values():
                ids.add(p["vk_id"])
    ids = [i for i in ids if i > 0]
    if not ids:
        return "Пока никто не писал боту."
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
# /написать — обращение ТОЛЬКО владельцу (OWNER_ID)
# ==========================================================
def cmd_write(msg, from_id, text):
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return "Формат: /написать текст"
    body_text = parts[1].strip()
    name = get_user_name(from_id)
    kb = VkKeyboard(inline=True)
    kb.add_openlink_button("💬 В чат", "https://vk.com/gim" + str(GROUP_ID) + "?sel=" + str(from_id))
    keyboard = kb.get_keyboard()
    header = "📩 Сообщение от [id" + str(from_id) + "|" + name + "]"
    sent = False
    cmid = msg.get("conversation_message_id")
    if cmid:
        try:
            vk.messages.send(
                user_id=OWNER_ID,
                message=header,
                random_id=get_random_id(),
                keyboard=keyboard,
                forward=json.dumps({
                    "peer_id": msg["peer_id"],
                    "conversation_message_ids": [cmid],
                    "is_reply": False,
                }),
            )
            sent = True
        except Exception as e:
            print("Не удалось переслать обращение, отправляю текстом:", repr(e))
    if not sent:
        try:
            vk.messages.send(
                user_id=OWNER_ID,
                message=header + ":\n\n" + body_text,
                random_id=get_random_id(),
                keyboard=keyboard,
            )
        except Exception as e:
            print("Ошибка отправки обращения:", repr(e))
            return "Не удалось отправить сообщение. Попробуйте позже."
    return "✅ Ваше сообщение отправлено."
# ==========================================================
# БАЛЛЫ ТЕКУЩЕГО ПОЛЬЗОВАТЕЛЯ
# ==========================================================
def cmd_points(user_id, n):
    with lock:
        player = find_by_id(n, user_id)
        if player is None:
            return "Вы не привязаны к нику. Обратитесь к администратору."
        return player["nick"] + ", у вас " + str(player["points"]) + " баллов."
def points_text(user_id):
    with lock:
        found = [(n, find_by_id(n, user_id)) for n in (1, 2)]
        found = [(n, p) for n, p in found if p is not None]
        if not found:
            return "Вы не привязаны к нику. Обратитесь к администратору."
        if len(found) == 1:
            p = found[0][1]
            return "💰 " + p["nick"] + ", у вас " + str(p["points"]) + " баллов."
        lines = ["💰 Ваши баллы:"]
        for n, p in found:
            lines.append(LIST_TITLE[n] + " — " + p["nick"] + ": " + str(p["points"]))
        return "\n".join(lines)
# ==========================================================
# МАГАЗИН (текстовый просмотр)
# ==========================================================
def cmd_shop(n):
    with lock:
        shop = list(S(n))
        if not shop:
            return "Магазин пуст."
        items = sorted(enumerate(shop, 1), key=lambda t: (t[1]["price"], t[0]))
        lines = [
            str(i) + ". " + item["name"] + " — " + str(item["price"]) + " баллов"
            for i, item in items
        ]
        return (
            "Магазин:\n" + "\n".join(lines)
            + "\n\nКупить можно кнопкой «🛒 Магазин» в меню (если кнопок нет — напишите /меню)."
        )
# ==========================================================
# КНОПКИ: клавиатуры
# ==========================================================
START_WORDS = {"начать", "начало", "start", "/start", "/начать", "меню", "/меню", "menu"}
MENU_TEXT = "Главное меню 👇"
WELCOME_TEXT = (
    "Привет! 👋\n"
    "Все действия теперь доступны кнопками ниже.\n"
    "Если кнопки пропадут — напишите /меню."
)
def btn(kb, label, c, color=VkKeyboardColor.SECONDARY, **extra):
    payload = {"c": c}
    payload.update(extra)
    kb.add_button(label[:40], color=color, payload=payload)
def menu_keyboard():
    kb = VkKeyboard(one_time=False)
    btn(kb, "🛒 Магазин", "shop", VkKeyboardColor.PRIMARY)
    btn(kb, "💰 Мои баллы", "points", VkKeyboardColor.POSITIVE)
    kb.add_line()
    btn(kb, "🏆 Топ", "top")
    btn(kb, "🌐 Общий топ", "top_all")
    kb.add_line()
    btn(kb, "❓ Помощь", "help")
    return kb.get_keyboard()
def choice_keyboard(options):
    """options: список (label, c, extra_dict). Внизу всегда «Назад»."""
    kb = VkKeyboard(one_time=False)
    for label, c, extra in options:
        btn(kb, label, c, VkKeyboardColor.PRIMARY, **extra)
        kb.add_line()
    btn(kb, "🔙 Назад", "menu")
    return kb.get_keyboard()
def user_lists(user_id):
    """В каких списках состоит пользователь (для магазина). Если ни в каком — [1]."""
    with lock:
        lists = [n for n in (1, 2) if find_by_id(n, user_id) is not None]
    return lists or [1]
def _item_label(item):
    suffix = " — " + str(item["price"])
    name = item["name"]
    room = 40 - len(suffix)
    if len(name) > room:
        name = name[:room - 1] + "…"
    return name + suffix
def shop_view(user_id, n, page=0):
    """Возвращает (текст, клавиатура) для страницы магазина."""
    with lock:
        shop = list(S(n))
        player = find_by_id(n, user_id)
    if not shop:
        return "Магазин пуст.", menu_keyboard()
    items = sorted(enumerate(shop), key=lambda t: (t[1]["price"], t[0]))
    pages = max(1, (len(items) + SHOP_PER_PAGE - 1) // SHOP_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * SHOP_PER_PAGE:(page + 1) * SHOP_PER_PAGE]

    kb = VkKeyboard(one_time=False)
    for idx, item in chunk:
        btn(kb, _item_label(item), "item", VkKeyboardColor.PRIMARY, l=n, i=idx, pg=page)
        kb.add_line()
    nav = False
    if page > 0:
        btn(kb, "⬅️", "shop", VkKeyboardColor.SECONDARY, l=n, pg=page - 1)
        nav = True
    if page < pages - 1:
        btn(kb, "➡️", "shop", VkKeyboardColor.SECONDARY, l=n, pg=page + 1)
        nav = True
    if nav:
        kb.add_line()
    btn(kb, "🔙 Назад", "menu")

    lines = ["🛒 Магазин"]
    if len([x for x in (1, 2) if find_by_id(x, user_id) is not None]) == 2:
        lines[0] += " (" + LIST_TITLE[n] + ")"
    if player is not None:
        lines.append("У вас: " + str(player["points"]) + " баллов")
    if pages > 1:
        lines.append("Страница " + str(page + 1) + " из " + str(pages))
    lines.append("")
    lines.append("Выберите товар 👇")
    return "\n".join(lines), kb.get_keyboard()
def item_view(user_id, n, idx, pg):
    """Экран подтверждения покупки."""
    with lock:
        player = find_by_id(n, user_id)
        if player is None:
            return "Вы не привязаны к нику. Обратитесь к администратору.", menu_keyboard()
        if player.get("vac_stage"):
            return "Сначала завершите оформление отпуска: ответьте на вопрос бота.", menu_keyboard()
        shop = S(n)
        if idx < 0 or idx >= len(shop):
            text, kb = shop_view(user_id, n, 0)
            return "Товар не найден, магазин обновился.\n\n" + text, kb
        item = shop[idx]
        name = item["name"]
        price = item["price"]
        points = player["points"]
    if points < price:
        text, kb = shop_view(user_id, n, pg)
        return "❌ Не хватает баллов. Нужно " + str(price) + ", у вас " + str(points) + ".\n\n" + text, kb
    crc = zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF
    text = (
        "🛍 Подтверждение покупки\n\n"
        "Товар: " + name + "\n"
        "Цена: " + str(price) + " баллов\n"
        "У вас: " + str(points) + " баллов\n"
        "Останется: " + str(points - price) + "\n\n"
        "Вы подтверждаете покупку?"
    )
    kb = VkKeyboard(one_time=False)
    btn(kb, "✅ Да, купить", "buy", VkKeyboardColor.POSITIVE, l=n, i=idx, p=price, h=crc)
    btn(kb, "❌ Отмена", "shop", VkKeyboardColor.NEGATIVE, l=n, pg=pg)
    return text, kb.get_keyboard()
# ==========================================================
# ПОКУПКА (после подтверждения)
# ==========================================================
def do_buy(user_id, n, idx, price, crc):
    now = time.time()
    key = (user_id, n, idx)
    last = LAST_BUY.get(key)
    if last is not None and now - last < 3:
        return "Покупка уже оформлена."
    with lock:
        player = find_by_id(n, user_id)
        if player is None:
            return "Вы не привязаны к нику. Обратитесь к администратору."
        if player.get("vac_stage"):
            return "Сначала завершите оформление отпуска: ответьте на вопрос бота."
        shop = S(n)
        if idx < 0 or idx >= len(shop):
            return "Товар не найден. Откройте магазин заново."
        item = shop[idx]
        item_name = item["name"]
        item_price = item["price"]
        if item_price != price or (zlib.crc32(item_name.encode("utf-8")) & 0xFFFFFFFF) != crc:
            return "Товар или его цена изменились. Откройте магазин заново."
        if player["points"] < item_price:
            return "Не хватает баллов. Нужно " + str(item_price) + ", у вас " + str(player["points"]) + "."
        LAST_BUY[key] = now
        name_lower = item_name.lower().strip()
        nick = player["nick"]
        num = idx + 1

        player["points"] -= item_price
        left = player["points"]

        if is_vacation_item(item_name):
            kind = "vacation"
        elif name_lower == "отгул":
            kind = "otgul"
        else:
            kind = "other"
        history = player.setdefault("purchases", [])
        history.append({
            "item": item_name,
            "price": item_price,
            "num": num,
            "kind": kind,
            "ts": datetime.now(MSK).isoformat(timespec="seconds"),
        })
        player["purchases"] = history[-30:]

        if kind == "vacation":
            # Админам НЕ пишем сейчас: уведомление уйдёт после ответа да/нет
            max_days = vacation_max_days_from_name(item_name)
            player["vac_stage"] = "date"
            player["vac_max"] = max_days
            player["vac_pending"] = {"num": num, "item": item_name, "price": item_price}
            save_data()
            return (
                "✅ Ваш отпуск оплачен! Напишите дату окончания отпуска в формате ДД.ММ "
                "(например 15.12). Максимум до " + str(max_days) + " дн. от сегодняшнего дня (МСК):"
            )

        if kind == "otgul":
            player["otgul"] = player.get("otgul", 0) + 1
            answer = "✅ Вы купили отгул! Осталось баллов: " + str(left) + "."
        else:
            answer = "✅ Вы купили: " + item_name + ". Осталось баллов: " + str(left) + "."

        save_data()
        text = (
            "[id" + str(user_id) + "|@" + nick + "] (" + nick + ") купил №" + str(num) + ": "
            + item_name + " (" + str(item_price) + " баллов). Выдайте ему."
        )
        notify_for_list(n, text)
        return answer
# ==========================================================
# ОБРАБОТКА НАЖАТИЙ НА КНОПКИ
# ==========================================================
def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
def button_response(user_id, payload):
    """Возвращает (текст, клавиатура)."""
    c = payload.get("c")

    if c == "menu":
        return MENU_TEXT, menu_keyboard()

    if c == "points":
        return points_text(user_id), menu_keyboard()

    if c == "help":
        return cmd_help(user_id), menu_keyboard()

    if c == "shop":
        lists = user_lists(user_id)
        n = _int(payload.get("l"), 0)
        if n not in (1, 2):
            if len(lists) == 2:
                return "Выберите магазин:", choice_keyboard([
                    ("🛒 Магазин /список", "shop", {"l": 1}),
                    ("🛒 Магазин /нлист", "shop", {"l": 2}),
                ])
            n = lists[0]
        return shop_view(user_id, n, _int(payload.get("pg"), 0))

    if c == "item":
        n = _int(payload.get("l"), 1)
        if n not in (1, 2):
            n = 1
        return item_view(user_id, n, _int(payload.get("i"), -1), _int(payload.get("pg"), 0))

    if c == "buy":
        n = _int(payload.get("l"), 1)
        if n not in (1, 2):
            n = 1
        answer = do_buy(
            user_id, n,
            _int(payload.get("i"), -1),
            _int(payload.get("p"), -1),
            _int(payload.get("h"), -1),
        )
        return answer, menu_keyboard()

    if c == "top":
        lists = top_lists(user_id)
        if not lists:
            return "Вы не привязаны к нику. Обратитесь к администратору.", menu_keyboard()
        if len(lists) == 1:
            return cmd_top(lists[0]), menu_keyboard()
        return "Какой топ показать?", choice_keyboard([
            ("🏆 Топ /список", "top_n", {"l": 1}),
            ("🏆 Топ /нлист", "top_n", {"l": 2}),
            ("🌐 Общий топ", "top_all", {}),
        ])

    if c == "top_n":
        n = _int(payload.get("l"), 0)
        lists = top_lists(user_id)
        if n not in (1, 2) or (n not in lists and user_id not in ALLOWED_IDS):
            return "Нет доступа к этому списку.", menu_keyboard()
        return cmd_top(n), menu_keyboard()

    if c == "top_all":
        if not top_lists(user_id):
            return "Вы не привязаны к нику. Обратитесь к администратору.", menu_keyboard()
        return cmd_top_all(), menu_keyboard()

    return MENU_TEXT, menu_keyboard()
# ==========================================================
# ОТПУСК: диалог после покупки (дата -> включительно да/нет)
# ==========================================================
YES_WORDS = {"да", "д", "yes", "y", "+", "дa"}
NO_WORDS = {"нет", "н", "no", "n", "-", "нeт"}
def handle_vacation_reply(player, user_id, n, text):
    """Вызывается под lock. Возвращает текст ответа игроку."""
    stage = player.get("vac_stage")
    pending = player.get("vac_pending") or {}
    max_days = player.get("vac_max", DEFAULT_VACATION_MAX_DAYS)
    answer = text.strip().lower()

    if stage == "date":
        end = parse_vacation_date(text)
        if end is None:
            return "Напишите дату в формате ДД.ММ (например 15.12):"
        days = (end - today_local()).days
        if days < 0:
            return "Эта дата уже прошла. Напишите дату в формате ДД.ММ (например 15.12):"
        if days > max_days:
            return (
                "Отпуск максимум до " + str(max_days) + " дн. от сегодняшнего дня. "
                "Напишите более раннюю дату (ДД.ММ):"
            )
        pending["end"] = end.isoformat()
        player["vac_pending"] = pending
        player["vac_stage"] = "incl"
        save_data()
        return "Отпуск до " + format_date(end) + " включительно? Ответьте: да / нет"

    if stage == "incl":
        if answer in YES_WORDS:
            incl = True
        elif answer in NO_WORDS:
            incl = False
        else:
            return "Ответьте «да» или «нет»: отпуск до этого числа включительно?"
        try:
            end = date.fromisoformat(pending.get("end", ""))
        except ValueError:
            player["vac_stage"] = "date"
            save_data()
            return "Напишите дату окончания отпуска в формате ДД.ММ (например 15.12):"
        date_text = format_date(end)
        player["vacation"] = date_text
        player["vac_end"] = end.isoformat()
        player["vac_incl"] = incl
        player["vac_stage"] = None
        player["vac_pending"] = None
        save_data()
        incl_text = "включительно" if incl else "не включительно"
        nick = player["nick"]
        item_name = pending.get("item") or "отпуск"
        item_price = pending.get("price", 0)
        num = pending.get("num", 0)
        notify_text = (
            "[id" + str(user_id) + "|@" + nick + "] (" + nick + ") купил отпуск до "
            + date_text + " " + incl_text + " (№" + str(num) + ": " + item_name
            + ", " + str(item_price) + " баллов). Выдайте ему."
        )
        notify_for_list(n, notify_text)
        return "✅ Отпуск зафиксирован до " + date_text + " " + incl_text + "."

    return None
# ==========================================================
# МАГАЗИН: добавить / удалить
# ==========================================================
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
    "Все действия — в кнопках меню. Если кнопок нет, напишите /меню.\n\n"
    "Кнопки:\n"
    "🛒 Магазин — товары и покупка\n"
    "💰 Мои баллы\n"
    "🏆 Топ — топ по баллам\n"
    "🌐 Общий топ\n\n"
    "Команды:\n"
    "/меню — показать кнопки\n"
    "/help — список команд"
)
ADMIN_HELP = (
    "Все команды:\n\n"
    "Для всех (в ЛС — кнопками, /меню показывает кнопки):\n"
    "/меню — показать кнопки\n"
    "/help — список команд\n\n"
    "Для администраторов (два независимых списка: /список и /нлист):\n"
    "/список — список участников с баллами\n"
    "/нлист — второй список участников\n"
    "/топ1 — топ по баллам списка /список\n"
    "/топ2 — топ по баллам списка /нлист\n"
    "/привязать1 @username Nick_Name — привязать в /список\n"
    "/привязать2 @username Nick_Name — привязать в /нлист\n"
    "/+магазин1 100 товар — добавить товар в магазин /список\n"
    "/+магазин2 100 товар — добавить товар в магазин /нлист\n"
    "/-магазин1 3 и /-магазин2 3 — удалить товар по номеру\n"
    "/разослать1 текст и /разослать2 текст — рассылка по списку\n"
    "/рассылка текст — рассылка всем, кто писал боту\n"
    "/выдать Nick_Name 5 — выдать баллы\n"
    "/забрать Nick_Name 5 — забрать баллы\n"
    "/откат Nick_Name или @username — откатить последнюю покупку (баллы не возвращаются)\n"
    "/откат Nick_Name возврат — откатить покупку и вернуть баллы\n"
    "/отвязать @username или Nick_Name — отвязать игрока\n"
    "/изменить Старый Новый — сменить ник\n"
    "(выдать/забрать/откат/отвязать/изменить сами находят список по нику; "
    "если ник есть в обоих — добавьте 1 или 2: /выдать2 Nick 5)\n"
    "/вд @username — выдать полный доступ админа\n"
    "/снятьвд @username — забрать доступ админа\n"
    "/вдв @username — выдать доступ только к списку /нлист\n"
    "/снятьвдв @username — забрать доступ к /нлист\n\n"
    "Отпуск: добавьте товар «отпуск максимум до 4 дней» через /+магазин. "
    "После покупки бот спросит дату и «включительно да/нет», затем уведомит админов. "
    "Время считается по МСК."
)
NADMIN_HELP = (
    "Все команды:\n\n"
    "Для всех (в ЛС — кнопками, /меню показывает кнопки):\n"
    "/меню — показать кнопки\n"
    "/help — список команд\n\n"
    "Для администратора /нлист:\n"
    "/нлист — список участников с баллами\n"
    "/выдать Nick_Name 5 — выдать баллы\n"
    "/забрать Nick_Name 5 — забрать баллы\n"
    "/откат Nick_Name или @username — откатить последнюю покупку (баллы не возвращаются)\n"
    "/откат Nick_Name возврат — откатить покупку и вернуть баллы\n"
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
    "/изменить", "/+магазин", "/-магазин", "/разослать", "/откат",
    "/баллы", "/магазин", "/купить", "/топ",
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
    if not found and command in ("/отвязать", "/убрать", "/откат"):
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
    # опечатка /октат = /откат
    if raw_cmd.startswith("/октат"):
        raw_cmd = "/откат" + raw_cmd[len("/октат"):]
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

    # Рассылка всем, кто писал боту — только общие админы
    if raw_cmd == "/рассылка":
        return cmd_broadcast_all(text) if is_common else None

    command, suffix = split_command(raw_cmd)
    if command not in LIST_COMMANDS or command in ("/баллы", "/магазин", "/купить", "/топ"):
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
    if command == "/откат":
        return cmd_rollback(args, n)
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
def parse_payload(msg):
    raw = msg.get("payload")
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None
def process_event(event):
    if event.type != VkBotEventType.MESSAGE_NEW:
        return
    msg = event.obj.message
    text = (msg.get("text") or "").strip()
    peer_id = msg["peer_id"]
    from_id = msg["from_id"]
    is_dm = (peer_id == from_id and from_id > 0)
    payload = parse_payload(msg)
    if DEBUG:
        print("Сообщение от " + str(from_id) + ": " + repr(text) + " payload=" + repr(payload))

    # Запоминаем всех, кто пишет боту в ЛС (для /рассылка)
    is_new = register_contact(from_id) if is_dm else False

    # === Нажатия на кнопки (только ЛС) ===
    if is_dm and payload:
        if payload.get("command") == "start":
            reply(peer_id, WELCOME_TEXT, menu_keyboard())
            return
        if payload.get("c"):
            try:
                answer, kb = button_response(from_id, payload)
            except Exception as e:
                print("Ошибка кнопки:", repr(e))
                answer, kb = "Произошла ошибка. Попробуйте позже.", menu_keyboard()
            reply(peer_id, answer, kb)
            return

    # === «Начать» / /меню ===
    if is_dm and text.lower() in START_WORDS:
        reply(peer_id, WELCOME_TEXT if is_new else MENU_TEXT, menu_keyboard())
        return

    # === Диалог отпуска: игрок пишет дату, затем да/нет (без команды) ===
    if not text.startswith("/"):
        handled = False
        with lock:
            for n in (1, 2):
                player = find_by_id(n, from_id)
                if player and player.get("vac_stage"):
                    answer = handle_vacation_reply(player, from_id, n, text)
                    if answer:
                        reply(peer_id, answer)
                    handled = True
                    break
        # новый пользователь написал что угодно — сразу показываем кнопки
        if is_dm and is_new and not handled:
            reply(peer_id, WELCOME_TEXT, menu_keyboard())
        return

    # новый пользователь начал с команды — тоже показываем кнопки
    if is_dm and is_new:
        reply(peer_id, WELCOME_TEXT, menu_keyboard())

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
            public_answer = "Покупки теперь через кнопки: нажмите «🛒 Магазин» в меню (если кнопок нет — напишите /меню)."
        elif raw_cmd in ("/топобщий", "/топ_общий"):
            public_answer = handle_top(from_id, None, ["общий"])
        elif command == "/топ":
            public_answer = handle_top(from_id, suffix, args)
        elif raw_cmd == "/я":
            public_answer = "Ваш VK ID: " + str(from_id)
        elif raw_cmd == "/написать":
            public_answer = cmd_write(msg, from_id, text)
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
        + " | контактов: " + str(len(CONTACTS))
    )
    print("Владелец обращений (/написать):", OWNER_ID)
    print("Сегодня (МСК):", today_local().isoformat())
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
