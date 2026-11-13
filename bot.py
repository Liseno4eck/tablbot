import os
import re
import json
import time
import zlib
import html as html_lib
import threading
import urllib.request
import urllib.error
from io import BytesIO
from datetime import datetime, timedelta, timezone, date
import vk_api
from concurrent.futures import ThreadPoolExecutor
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.longpoll import VkLongPoll, VkEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.utils import get_random_id

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except Exception:
    PIL_OK = False
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
# Владелец: ТОЛЬКО ему приходят обращения из /написать и только он может отвечать через /ответить.
# Поменяйте на свой VK ID (или задайте переменную окружения OWNER_ID)
OWNER_ID = int(os.environ.get("OWNER_ID", "875762552"))
DATA_FILE = os.environ.get("DATA_FILE", "data.json")
GIST_TOKEN_FALLBACK = ""
GIST_ID_FALLBACK = ""
# Всё время (даты отпуска) считается по Москве
MSK = timezone(timedelta(hours=3))
# Максимум дней отпуска от сегодняшнего дня (для /отпуск)
DEFAULT_VACATION_MAX_DAYS = 7
# Сколько мест показывать в /топ
TOP_LIMIT = 10
# Сколько товаров на одной странице кнопок магазина
SHOP_PER_PAGE = 6
# Сколько последних покупок хранить у каждого игрока (для /откат по номеру)
PURCHASE_HISTORY_LIMIT = 200
# Папка для шрифтов (для картинки-справки)
FONT_DIR = os.environ.get("FONT_DIR", "fonts")
# ==========================================================
# НОРМА ОТЧЁТОВ: сколько скринов и сколько баллов выдавать
#   <  REPORT_MIN_SHOTS скринов  -> баллов НЕ выдаём (0)
#   в REPORT_CHECK_SHOTS скринов -> REPORT_PART_POINTS, НО только после
#      проверки: бот предлагает показать скрины и затем спрашивает, выдавать ли
#   >= REPORT_FULL_SHOTS скринов -> REPORT_FULL_POINTS баллов
#   от REPORT_MIN_SHOTS до REPORT_FULL_SHOTS-1 -> REPORT_PART_POINTS баллов
#   0 скринов / отгул / отпуск   -> баллы не выдаются
#   Повторная проверка за тот же день: выдаётся только РАЗНИЦА между новой
#   нормой и тем, что уже было выдано (переплата невозможна).
# ==========================================================
REPORT_FULL_SHOTS = 10
REPORT_FULL_POINTS = 10
REPORT_PART_POINTS = 7
REPORT_MIN_SHOTS = 4          # меньше 4 скринов — 0 баллов
REPORT_CHECK_SHOTS = (4, 5)   # 4-5 скринов — 7 баллов, но только после показа скринов и подтверждения
# ==========================================================
# СТРАНИЧНЫЙ БОТ (аккаунт пользователя, подключённый командой +токен)
# ==========================================================
PAGE_TOKEN = None        # токен страничного бота
PAGE_GROUP_ID = None     # (не используется для user-токена)
PAGE_USER_ID = None      # VK ID пользователя, который подключил бота
PAGE_VK = None           # VkApi-сессия для второго токена
PAGE_LONGPOLL = None
PAGE_READY = False       # флаг: страничный бот подключён и готов
PAGE_STATE = {}          # peer_id (чат, где ввели команду) -> состояние проверки (сторона основного бота)
PAGE_NICKS = {}
PAGE_REPORTS = {}
PAGE_NICKS_GIST = "nicks_reports.json"
PAGE_GEN = 0             # «поколение» страничного бота: при смене токена старый поток сам завершается
PAGE_THREAD = None
PAGE_JOB = None          # текущая задача страничного бота (сторона страничного бота)
# Ожидаем нажатие кнопки в чате: peer_id -> {"stage", "n", "date", "lines", ...}
REPORT_AWAIT = {}
REPORT_AWAIT_TIMEOUT = 1800
page_lock = threading.Lock()
PAGE_TIMEOUT = 240       # сколько секунд ждём результат проверки
# ==========================================================
# ГДЕ ПРОВЕРЯТЬ ОТЧЁТЫ: сообщество + два обсуждения (топика)
# ==========================================================
# Метка, которую страничный бот пишет в обсуждении после проверки.
# При повторной проверке за тот же день учитываются только комментарии ПОСЛЕ неё.
MARKER_TEXT = "все выше отчеты проверены"
# ID сообщества, в котором игроки скидывают отчёты.
REPORT_GROUP_ID = int(os.environ.get("REPORT_GROUP_ID", "0") or 0)
# Обсуждение, где проверяются отчёты для /от  (список /нлист)
REPORT_TOPIC_ID_OT = int(os.environ.get("REPORT_TOPIC_ID_OT", "0") or 0)
# Обсуждение, где проверяются отчёты для /отчеты  (список /список)
REPORT_TOPIC_ID_OTCHETY = int(os.environ.get("REPORT_TOPIC_ID_OTCHETY", "0") or 0)
# ==========================================================
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
NICKS_GIST_ENABLED = bool(GIST_TOKEN and GIST_ID)
ON_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
DEBUG = False
# ==========================================================
# ПОДКЛЮЧЕНИЕ К VK
# ==========================================================
# Inline-кнопки (внутри сообщения) поддерживаются VK начиная с API 5.104.
# По умолчанию vk_api шлёт 5.92 и VK игнорирует "inline": true, поэтому
# явно поднимаем версию.
VK_API_VERSION = "5.199"
vk_session = vk_api.VkApi(token=VK_TOKEN, api_version=VK_API_VERSION)
vk = vk_session.get_api()
executor = ThreadPoolExecutor(max_workers=8)
# ==========================================================
# ХРАНИЛИЩЕ
# Список 1 (/список):  players, shop, admins
# Список 2 (/нлист):   nplayers, nshop, nadmins
# contacts: все, кто писал боту в ЛС (для /рассылка)
# purchase_counter: сквозной счётчик номеров покупок (общий для обоих списков)
# contest: конкурс — период и заработанные баллы (для /конкурс и /итоги)
# players: nick_lower -> {
#   nick, vk_id, points, vacation (строка для показа), otgul,
#   otgul_end (ISO дата последнего дня отгула или None),
#   vac_stage (None | "date" | "incl"),
#   vac_max (макс. дней), vac_pending (данные покупки до подтверждения),
#   vac_end (ISO дата окончания), vac_incl (True/False/None),
#   purchases (история покупок для /откат, у каждой есть "id" — номер покупки)
# }
# last_admin_msg: "vk_id" -> admin_id — какой админ последний написал этому игроку
# (через /смс или /ответить). Именно этому админу придёт следующее /написать от игрока.
# ==========================================================
lock = threading.RLock()
save_lock = threading.Lock()
data = {
    "players": {}, "shop": [], "admins": [],
    "nplayers": {}, "nshop": [], "nadmins": [],
    "contacts": [],
    "purchase_counter": 0,
    # секретное подключение страничного бота
    "page": {"token": "", "user_id": 0, "group_id": 0},
    # где проверять отчёты (переопределяет REPORT_* из шапки)
    "report_settings": {"group_id": 0, "topic_ot": 0, "topic_otchety": 0},
    # отчёты: "дд.мм.гггг" -> {list_type: {nick: статус}}
    "reports": {},
    # кому уже выдали баллы за отчёт: "дд.мм.гггг" -> {list_type: {nick: сколько выдано}}
    "points_awarded": {},
    # ники для отчётов (складываются в Gist)
    "report_nicks": {},
    # конкурс: период и заработанные баллы
    "contest": {"start": "", "end": "", "earned": {}},
    # последний админ, написавший игроку (через /смс или /ответить)
    "last_admin_msg": {},
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
# ==========================================================
# GIST: отчёты и ники (отдельный файл)
# ==========================================================
NICKS_GIST_FILENAME = "nicks_reports.json"
def _gist_request_nicks(method, url, body=None):
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
def gist_nicks_load():
    """Загружает данные отчётов из Gist (отдельный файл)."""
    if not NICKS_GIST_ENABLED:
        return {}
    try:
        text = _gist_request_nicks("GET", "https://api.github.com/gists/" + GIST_ID)
        info = json.loads(text)
        files = info.get("files") or {}
        file_info = files.get(NICKS_GIST_FILENAME)
        if file_info is None:
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
            raise ValueError("содержимое nicks_reports.json должно быть JSON-объектом")
        return parsed
    except Exception as e:
        print("Не удалось загрузить отчёты из Gist:", repr(e))
        return {}
def gist_nicks_save(data_to_save):
    """Сохраняет данные отчётов в Gist (отдельный файл)."""
    if not NICKS_GIST_ENABLED:
        return True
    try:
        payload = json.dumps(data_to_save, ensure_ascii=False, indent=2)
        _gist_request_nicks(
            "PATCH",
            "https://api.github.com/gists/" + GIST_ID,
            {"files": {NICKS_GIST_FILENAME: {"content": payload}}},
        )
        print("Данные отчётов сохранены в Gist.")
        return True
    except Exception as e:
        print("Ошибка сохранения отчётов в Gist:", repr(e))
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
def _norm_date_field(raw):
    """ISO-строка даты -> та же строка, если дата корректна. Иначе None."""
    if not raw:
        return None
    text = str(raw).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except (ValueError, TypeError):
        return None
def _norm_date_field(raw):
    """ISO-дата в строку ISO или None (пусто/битое -> None)."""
    if not raw:
        return None
    text = str(raw).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
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
            try:
                pid = int(it.get("id", 0) or 0)
            except (TypeError, ValueError):
                pid = 0
            kind = it.get("kind")
            if kind not in ("vacation", "otgul", "other"):
                kind = "other"
            result.append({
                "id": pid,
                "item": str(it.get("item") or ""),
                "price": price,
                "num": num,
                "kind": kind,
                "ts": str(it.get("ts") or ""),
            })
    return result[-PURCHASE_HISTORY_LIMIT:]
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
            # отгул теперь хранится как дата последнего дня (ISO), а не счётчик покупок
            otgul_end = _norm_date_field(value.get("otgul_end"))
            if otgul > 0 and not otgul_end:
                otgul_end = today_local().isoformat()
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
                "otgul_end": otgul_end,
                "vac_stage": vac_stage,
                "vac_max": vac_max,
                "vac_pending": vac_pending,
                "vac_end": value.get("vac_end"),
                "vac_incl": vac_incl,
                "purchases": _norm_purchases(value.get("purchases")),
            }
    return players
def _norm_shop(raw_shop):
    """Магазин. Товары «отгул» и «отпуск» больше не продаются — убираются из списка."""
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
            if not name or price <= 0:
                continue
            if is_vacation_item(name) or name.lower() == "отгул":
                continue
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
def _norm_page(raw_page):
    """Нормализует сохранённое секретное подключение страничного бота."""
    raw_page = raw_page if isinstance(raw_page, dict) else {}
    token = str(raw_page.get("token") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{10,512}", token):
        token = ""
    try:
        user_id = int(raw_page.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    try:
        group_id = int(raw_page.get("group_id") or 0)
    except (TypeError, ValueError):
        group_id = 0
    return {"token": token, "user_id": user_id if user_id > 0 else 0,
            "group_id": group_id if group_id > 0 else 0}
def _norm_reports(raw_reports):
    """"дд.мм.гггг" -> {"1": {nick: "есть отчет N скринов"/"нет отчета"/"отгул"}, "2": {...}}"""
    result = {}
    if not isinstance(raw_reports, dict):
        return result
    for day, lists in raw_reports.items():
        day = str(day).strip()
        if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", day) or not isinstance(lists, dict):
            continue
        entry = {}
        for key in ("1", "2"):
            block = lists.get(key)
            if not isinstance(block, dict):
                continue
            nick_map = {}
            for nick, status in block.items():
                nick = str(nick).strip()
                if nick:
                    nick_map[nick] = str(status).strip()
            if nick_map:
                entry[key] = nick_map
        if entry:
            result[day] = entry
    return result
def _norm_points_awarded(raw):
    """"дд.мм.гггг" -> {"1": {nick: сколько баллов уже выдано}, "2": {...}}.
    Старый формат (просто список ников) -> сумма неизвестна (None): повторно не выдаём."""
    result = {}
    if not isinstance(raw, dict):
        return result
    for day, lists in raw.items():
        day = str(day).strip()
        if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", day) or not isinstance(lists, dict):
            continue
        entry = {}
        for key in ("1", "2"):
            block = lists.get(key)
            nick_map = {}
            if isinstance(block, dict):
                for nick, pts in block.items():
                    nick = str(nick).strip()
                    if not nick:
                        continue
                    try:
                        nick_map[nick] = int(pts)
                    except (TypeError, ValueError):
                        nick_map[nick] = None
            elif isinstance(block, list):
                for nick in block:
                    nick = str(nick).strip()
                    if nick:
                        nick_map[nick] = None
            if nick_map:
                entry[key] = nick_map
        if entry:
            result[day] = entry
    return result
def _norm_report_settings(raw):
    """Настройки «где проверять отчёты»: group_id, topic_ot, topic_otchety."""
    raw = raw if isinstance(raw, dict) else {}
    def _pid(value, fallback):
        try:
            value = int(value or 0)
        except (TypeError, ValueError):
            return fallback
        return value if value > 0 else fallback
    return {
        "group_id": _pid(raw.get("group_id"), REPORT_GROUP_ID),
        "topic_ot": _pid(raw.get("topic_ot"), REPORT_TOPIC_ID_OT),
        "topic_otchety": _pid(raw.get("topic_otchety"), REPORT_TOPIC_ID_OTCHETY),
    }
def _norm_contest(raw):
    """Конкурс: start/end (ISO даты) и earned: "список:vk_id" -> {nick, n, pts}."""
    raw = raw if isinstance(raw, dict) else {}
    start = str(raw.get("start") or "")
    end = str(raw.get("end") or "")
    try:
        date.fromisoformat(start)
        date.fromisoformat(end)
    except ValueError:
        start = ""
        end = ""
    earned = {}
    src = raw.get("earned")
    if isinstance(src, dict):
        for key, value in src.items():
            if not isinstance(value, dict):
                continue
            nick = str(value.get("nick") or "").strip()
            if not nick:
                continue
            try:
                pts = int(value.get("pts", 0))
            except (TypeError, ValueError):
                pts = 0
            try:
                n = int(value.get("n", 1))
            except (TypeError, ValueError):
                n = 1
            earned[str(key)] = {"nick": nick, "n": 2 if n == 2 else 1, "pts": max(0, pts)}
    return {"start": start, "end": end, "earned": earned}
def _norm_last_admin_msg(raw):
    """Кто из админов последний писал игроку: "vk_id" -> admin_id."""
    result = {}
    if not isinstance(raw, dict):
        return result
    for key, value in raw.items():
        key = str(key).strip()
        if not re.fullmatch(r"\d+", key):
            continue
        try:
            admin_id = int(value)
        except (TypeError, ValueError):
            continue
        if admin_id > 0:
            result[key] = admin_id
    return result
def _assign_purchase_ids(players1, players2, counter):
    """Старым покупкам без номера выдаёт номера (по времени). Возвращает новый счётчик."""
    all_purchases = []
    for players in (players1, players2):
        for p in players.values():
            for pur in p.get("purchases", []):
                if pur.get("id", 0) > counter:
                    counter = pur["id"]
                all_purchases.append(pur)
    legacy = [pur for pur in all_purchases if pur.get("id", 0) <= 0]
    legacy.sort(key=lambda pur: pur.get("ts") or "")
    for pur in legacy:
        counter += 1
        pur["id"] = counter
    return counter
def _normalize_loaded(loaded):
    players1 = _norm_players(loaded.get("players"))
    players2 = _norm_players(loaded.get("nplayers"))
    try:
        counter = int(loaded.get("purchase_counter", 0) or 0)
    except (TypeError, ValueError):
        counter = 0
    counter = _assign_purchase_ids(players1, players2, max(0, counter))
    return {
        "players": players1,
        "shop": _norm_shop(loaded.get("shop")),
        "admins": _norm_ids(loaded.get("admins")),
        "nplayers": players2,
        "nshop": _norm_shop(loaded.get("nshop")),
        "nadmins": _norm_ids(loaded.get("nadmins")),
        "contacts": list(dict.fromkeys(_norm_ids(loaded.get("contacts")))),
        "purchase_counter": counter,
        "page": _norm_page(loaded.get("page")),
        "report_settings": _norm_report_settings(loaded.get("report_settings")),
        "reports": _norm_reports(loaded.get("reports")),
        "report_nicks": _norm_reports(loaded.get("report_nicks")),
        "contest": _norm_contest(loaded.get("contest")),
        "last_admin_msg": _norm_last_admin_msg(loaded.get("last_admin_msg")),
        "points_awarded": _norm_points_awarded(loaded.get("points_awarded")),
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
    data["purchase_counter"] = normalized["purchase_counter"]
    data["page"] = normalized["page"]
    data["report_settings"] = normalized["report_settings"]
    data["reports"] = normalized["reports"]
    data["report_nicks"] = normalized["report_nicks"]
    data["contest"] = normalized["contest"]
    data["last_admin_msg"] = normalized["last_admin_msg"]
    data["points_awarded"] = normalized["points_awarded"]
    ALLOWED_IDS.clear()
    ALLOWED_IDS.update(BASE_ADMIN_IDS)
    ALLOWED_IDS.update(data["admins"])
    NADMIN_IDS.clear()
    NADMIN_IDS.update(data["nadmins"])
    CONTACTS.clear()
    CONTACTS.update(data["contacts"])
    # Восстанавливаем страничный бот из сохранённых данных
    page = data.get("page", {})
    if page.get("token"):
        global PAGE_TOKEN, PAGE_VK, PAGE_GROUP_ID, PAGE_READY
        PAGE_TOKEN = page.get("token")
        PAGE_GROUP_ID = page.get("group_id")
        PAGE_READY = True
        try:
            PAGE_VK = vk_api.VkApi(token=PAGE_TOKEN)
            print("Страничный бот восстановлен из файла: user=" + str(page.get("user_id")))
        except Exception as e:
            print("Не удалось восстановить страничный бот:", repr(e))
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

def otgul_active(player):
    """Отгул: активен, если today <= otgul_end. Просроченный сбрасывается.
    Возвращает (active, changed)."""
    raw = player.get("otgul_end")
    if not raw:
        return False, False
    try:
        end = date.fromisoformat(raw)
    except ValueError:
        player["otgul_end"] = None
        return False, True
    if today_local() > end:
        player["otgul_end"] = None
        return False, True
    return True, False
# ==========================================================
# КОМАНДЫ АДМИНА: /отгул, /отпуск, /смс
# ==========================================================
def _resolve_player(target, is_common):
    """Находит игрока по нику, @username или vk.com/id... Возвращает (n, player) или (None, None)."""
    lists = (1, 2) if is_common else (2,)
    with lock:
        for n in lists:
            p = find_by_nick(n, target)
            if p:
                return n, p
    uid = resolve_user(target)
    if uid:
        with lock:
            for n in lists:
                p = find_by_id(n, uid)
                if p:
                    return n, p
    return None, None
def cmd_otgul(args, is_common):
    """/отгул Ник — выдать отгул. /отгул Ник снять (или повторный вызов) — снять."""
    if len(args) < 1:
        return "Формат: /отгул Ник_Name или @username или ссылка на ВК\nСнять: /отгул Ник_Name снять"
    n, player = _resolve_player(args[0], is_common)
    if player is None:
        return "Игрок " + args[0] + " не найден."
    want_remove = len(args) > 1 and args[1].lower() in ("снять", "убрать", "стоп", "off")
    if want_remove and not player.get("otgul_end"):
        return "У " + player["nick"] + " отгула нет (" + LIST_TITLE[n] + ")."
    with lock:
        if player.get("otgul_end") and (want_remove or len(args) == 1):
            player["otgul_end"] = None
            save_data()
            return "✅ Отгул снят: " + player["nick"] + " (" + LIST_TITLE[n] + ")."
        player["otgul_end"] = today_local().isoformat()
        save_data()
        return "✅ Отгул выдан на сегодня: " + player["nick"] + " (" + LIST_TITLE[n] + ")."
def cmd_otpusk(args, is_common):
    """/отпуск — показать кто в отпуске/отгуле.
    /отпуск Ник ДД.ММ — выдать отпуск до даты. /отпуск Ник снять — снять."""
    if not args:
        # показать всех кто в отпуске или отгуле
        lists = (1, 2) if is_common else (2,)
        lines = ["📋 Отпуска и отгулы:"]
        found = False
        for n in lists:
            with lock:
                players = list(P(n).values())
            for p in players:
                vac = p.get("vacation")
                if vac:
                    lines.append(LIST_TITLE[n] + ": " + p["nick"] + " — отпуск до " + str(vac))
                    found = True
                if p.get("otgul_end"):
                    lines.append(LIST_TITLE[n] + ": " + p["nick"] + " — отгул")
                    found = True
        if not found:
            lines.append("  Пока никого нет.")
        return "\n".join(lines)
    n, player = _resolve_player(args[0], is_common)
    if player is None:
        return "Игрок " + args[0] + " не найден."
    want_remove = len(args) > 1 and args[1].lower() in ("снять", "убрать", "стоп", "off")
    if want_remove and not player.get("vacation"):
        return "У " + player["nick"] + " отпуска нет (" + LIST_TITLE[n] + ")."
    if not player.get("vacation"):
        if len(args) < 2:
            return "Формат: /отпуск Ник 15.12 (или /отпуск Ник 15.12.2026)"
        end = parse_vacation_date(args[1])
        if end is None:
            return "Не понял дату. Напишите в формате ДД.ММ или ДД.ММ.ГГГГ."
        if end < today_local():
            return "Эта дата уже прошла."
        with lock:
            player["vacation"] = format_date(end)
            player["vac_end"] = end.isoformat()
            player["vac_incl"] = True
            player["vac_stage"] = None
            player["vac_pending"] = None
            player["otgul_end"] = None
            save_data()
            return "✅ Отпуск выдан до " + format_date(end) + ": " + player["nick"] + " (" + LIST_TITLE[n] + ")."
    with lock:
        _clear_vacation(player)
        save_data()
        return "✅ Отпуск снят: " + player["nick"] + " (" + LIST_TITLE[n] + ")."
def cmd_sms(args, is_common, admin_id):
    """/смс Ник Текст — отправить сообщение игроку от имени администратора."""
    if len(args) < 2:
        return "Формат: /смс Ник_Name Текст сообщения"
    n, player = _resolve_player(args[0], is_common)
    if player is None:
        return "Игрок " + args[0] + " не найден."
    text = " ".join(args[1:])
    vk_id = player["vk_id"]
    nick = player["nick"]
    message = "💬 Сообщение от администратора:\n" + text
    try:
        vk.messages.send(user_id=vk_id, message=message, random_id=get_random_id())
    except Exception as e:
        print("Ошибка отправки /смс:", repr(e))
        return "❌ Не удалось отправить сообщение " + nick + " (" + repr(e) + ")"
    with lock:
        data.setdefault("last_admin_msg", {})[str(vk_id)] = admin_id
        save_data()
    return "✅ Сообщение отправлено: " + nick + " (" + LIST_TITLE[n] + ")."
# ==========================================================
# ОТПРАВКА СООБЩЕНИЙ
# ==========================================================
def reply(peer_id, text, keyboard=None, attachment=None):
    """Отправляет сообщение и возвращает его ID (None — если не удалось).
    keyboard=None -> без клавиатуры, keyboard="" -> убрать клавиатуру,
    keyboard=JSON -> установить клавиатуру."""
    try:
        params = {"peer_id": peer_id, "message": str(text), "random_id": get_random_id()}
        if keyboard is not None:
            params["keyboard"] = keyboard
        if attachment:
            params["attachment"] = attachment
        return vk.messages.send(**params)
    except Exception as e:
        print("Ошибка отправки сообщения:", repr(e))
        return None
def edit_message(peer_id, message_id, text, keyboard=None):
    """Редактирует сообщение бота. keyboard=None -> кнопки убираются совсем."""
    if not message_id:
        return False
    try:
        vk.messages.edit(
            peer_id=peer_id,
            message_id=int(message_id),
            message=str(text),
            keyboard=keyboard if keyboard is not None else hide_inline_keyboard(),
        )
        return True
    except Exception as e:
        print("Ошибка редактирования сообщения:", repr(e))
        return False
def split_long_text(text, limit=3800):
    """Режет длинный текст по строкам на куски до limit символов."""
    chunks = []
    cur = ""
    for line in str(text).split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        add = line if not cur else "\n" + line
        if len(cur) + len(add) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur += add
    if cur:
        chunks.append(cur)
    return chunks or [""]
def reply_long(peer_id, text):
    for chunk in split_long_text(text):
        reply(peer_id, chunk)
        time.sleep(0.2)
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
def _notify_rollback_worker(n, text, exclude_id):
    ids = set(ALLOWED_IDS)
    if n == 2:
        ids |= set(NADMIN_IDS)
    ids.discard(exclude_id)
    for admin_id in ids:
        send_to_user(admin_id, text)
def notify_rollback(n, text, exclude_id):
    """Сообщение об откате всем админам (кроме того, кто откатил).
    Для покупок из /нлист — ещё и админам /вдв."""
    executor.submit(_notify_rollback_worker, n, text, exclude_id)
# ==========================================================
# СТРАНИЧНЫЙ БОТ: отправка / получение
# ==========================================================
def _get_page_vk():
    """Возвращает VkApi-сессию для страничного бота (создаёт, если нужно)."""
    global PAGE_VK, PAGE_TOKEN, PAGE_GROUP_ID
    page = data.get("page", {})
    token = page.get("token", "")
    group_id = page.get("group_id", 0)
    if token and token != PAGE_TOKEN:
        PAGE_TOKEN = token
        PAGE_VK = vk_api.VkApi(token=token)
        PAGE_GROUP_ID = group_id
    elif token and PAGE_VK is None:
        PAGE_VK = vk_api.VkApi(token=token)
        PAGE_GROUP_ID = group_id
    return PAGE_VK
def send_to_page(user_id, text, keyboard=None):
    """Основной бот (токен сообщества) пишет страничному боту в ЛС."""
    try:
        params = {"user_id": user_id, "message": str(text), "random_id": get_random_id()}
        if keyboard:
            params["keyboard"] = keyboard
        vk.messages.send(**params)
        return True
    except Exception as e:
        print("Ошибка отправки страничному боту:", repr(e))
        return False
def send_page_message(user_id, text, keyboard=None):
    """Отправляет сообщение от страничного бота (через второй токен)."""
    p_vk = _get_page_vk()
    if p_vk is None:
        print("Страничный бот не подключён.")
        return False
    try:
        params = {"user_id": user_id, "message": str(text), "random_id": get_random_id()}
        if keyboard:
            params["keyboard"] = keyboard
        p_vk.get_api().messages.send(**params)
        return True
    except Exception as e:
        print("Ошибка отправки от страничного бота:", repr(e))
        return False
def is_page_connected():
    """Проверяет, подключён ли страничный бот."""
    page = data.get("page", {})
    token = page.get("token", "")
    user_id = page.get("user_id", 0)
    return bool(token) and bool(user_id)
def get_page_user_id():
    """Возвращает VK ID пользователя, подключившего страничный бот."""
    page = data.get("page", {})
    return page.get("user_id", 0)
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
# КОНКУРС: подсчёт заработанных баллов за период (по МСК)
# Считаются только начисления (/выдать). Если баллы забрали (/забрать) —
# вычитаются. Покупки в магазине и откаты с возвратом НЕ влияют.
# Ключ записи: "номер_списка:vk_id", так что каждый ник считается отдельно.
# ==========================================================
def parse_contest_date(text):
    """'15.11.2026' или '15.11' (год — текущий) -> date. Иначе None."""
    match = re.fullmatch(DATE_RE, text.strip())
    if not match:
        return None
    try:
        year_raw = match.group(3)
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
        else:
            year = today_local().year
        return date(year, int(match.group(2)), int(match.group(1)))
    except ValueError:
        return None
def contest_period():
    """(start, end) как date или (None, None), если конкурс не задан."""
    contest = data.get("contest") or {}
    try:
        return date.fromisoformat(contest.get("start", "")), date.fromisoformat(contest.get("end", ""))
    except ValueError:
        return None, None
def contest_running():
    """Идёт ли конкурс прямо сейчас (сегодня по МСК между датами включительно)."""
    start, end = contest_period()
    if start is None:
        return False
    return start <= today_local() <= end
def contest_record(n, player, delta):
    """Вызывать под lock при изменении баллов выдачей/забором. delta > 0 — выдали, < 0 — забрали."""
    if not contest_running():
        return
    contest = data.setdefault("contest", {"start": "", "end": "", "earned": {}})
    earned = contest.setdefault("earned", {})
    key = str(n) + ":" + str(player["vk_id"])
    entry = earned.get(key)
    if entry is None:
        entry = {"nick": player["nick"], "n": n, "pts": 0}
        earned[key] = entry
    entry["nick"] = player["nick"]
    entry["pts"] = max(0, int(entry.get("pts", 0)) + delta)
def contest_rename(n, vk_id, new_nick):
    """При смене ника обновляем имя в конкурсе (под lock)."""
    entry = (data.get("contest") or {}).get("earned", {}).get(str(n) + ":" + str(vk_id))
    if entry is not None:
        entry["nick"] = new_nick
def _fmt_full_date(d):
    return d.strftime("%d.%m.%Y")
def cmd_contest(args):
    """/конкурс дд.мм.гггг дд.мм.гггг — запустить; /конкурс — статус; /конкурс стоп — сбросить."""
    usage = (
        "Формат: /конкурс дата_начала дата_конца\n"
        "Например: /конкурс 10.11.2026 20.11.2026 (или /конкурс 10.11 20.11)\n"
        "/конкурс — показать текущий конкурс\n"
        "/конкурс стоп — остановить и сбросить конкурс"
    )
    if not args:
        with lock:
            start, end = contest_period()
            total = len((data.get("contest") or {}).get("earned", {}))
        if start is None:
            return "Конкурс не запущен.\n\n" + usage
        today = today_local()
        if today < start:
            status = "ещё не начался"
        elif today <= end:
            status = "идёт"
        else:
            status = "закончился"
        return (
            "🏁 Конкурс: " + _fmt_full_date(start) + " — " + _fmt_full_date(end) + " (" + status + ")\n"
            "Игроков с баллами: " + str(total) + "\n"
            "Результаты: /итоги"
        )
    if args[0].lower() in ("стоп", "сброс", "stop", "отмена"):
        with lock:
            data["contest"] = {"start": "", "end": "", "earned": {}}
            save_data()
        return "✅ Конкурс остановлен, данные сброшены."
    if len(args) != 2:
        return usage
    start = parse_contest_date(args[0])
    end = parse_contest_date(args[1])
    if start is None or end is None:
        return "Не понял даты.\n\n" + usage
    if end < start:
        return "Дата конца не может быть раньше даты начала."
    with lock:
        data["contest"] = {"start": start.isoformat(), "end": end.isoformat(), "earned": {}}
        save_data()
    today = today_local()
    if today < start:
        note = "Подсчёт начнётся " + _fmt_full_date(start) + " (МСК)."
    elif today <= end:
        note = "Подсчёт идёт с этого момента."
    else:
        note = "⚠️ Конкурс уже закончился, новые баллы считаться не будут."
    return (
        "✅ Конкурс запущен: " + _fmt_full_date(start) + " — " + _fmt_full_date(end) + ".\n"
        "Считаются только выданные баллы (/выдать); если баллы забрали (/забрать) — вычитаются, "
        "покупки в магазине не вычитаются. Время по МСК.\n"
        + note + "\n"
        "Итоги: /итоги"
    )
def cmd_contest_results():
    """/итоги — все ники из обоих списков по заработанным баллам, от большего к меньшему."""
    with lock:
        start, end = contest_period()
        if start is None:
            return "Конкурс не запущен. Запустить: /конкурс дата_начала дата_конца"
        earned = dict((data.get("contest") or {}).get("earned", {}))
        rows = {}
        for key, e in earned.items():
            rows[key] = (e.get("nick", "?"), int(e.get("n", 1)), int(e.get("pts", 0)))
        # добавляем всех текущих игроков, даже если у них 0
        for n in (1, 2):
            for p in P(n).values():
                key = str(n) + ":" + str(p["vk_id"])
                if key not in rows:
                    rows[key] = (p["nick"], n, 0)
    today = today_local()
    if today < start:
        status = "ещё не начался"
    elif today <= end:
        status = "идёт"
    else:
        status = "закончился"
    items = sorted(rows.values(), key=lambda r: (-r[2], r[0].lower()))
    if not items:
        return "Пока нет игроков."
    lines = [
        "🏆 Итоги конкурса " + _fmt_full_date(start) + " — " + _fmt_full_date(end) + " (" + status + "):",
        "",
    ]
    for i, (nick, n, pts) in enumerate(items, 1):
        lines.append(str(i) + ". " + nick + " — " + str(pts) + " (" + LIST_TITLE[n] + ")")
    return "\n".join(lines)
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
        contest_record(n, player, sign * amount)
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
        "otgul_end": None,
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
        contest_rename(n, player["vk_id"], new)
        save_data()
        return "Ник изменён: " + old_nick + " -> " + new + "."
# ==========================================================
# ОТКАТ покупки ПО НОМЕРУ (/откат 5 или /откат 5 вернуть)
# ==========================================================
RETURN_WORDS = {"возврат", "возвращ", "возвратить", "вернуть"}
def cmd_rollback(args, admin_id, is_common):
    """Откатывает покупку по её номеру. Общий админ — любую, админ /вдв — только из /нлист."""
    usage = (
        "Формат: /откат НОМЕР_ПОКУПКИ\n"
        "/откат НОМЕР_ПОКУПКИ вернуть — откатить и вернуть баллы\n"
        "Например: /откат 2 вернуть"
    )
    if not args or not re.fullmatch(r"\d+", args[0]):
        return usage
    pid = int(args[0])
    if pid <= 0:
        return usage
    rest = [a.lower() for a in args[1:]]
    refund = any(a in RETURN_WORDS for a in rest)
    if any(a not in RETURN_WORDS for a in rest):
        return usage

    admin_name = get_user_name(admin_id)
    lists = (1, 2) if is_common else (2,)

    with lock:
        player = None
        found_n = None
        last = None
        for n in lists:
            for p in P(n).values():
                for pur in (p.get("purchases") or []):
                    if pur.get("id") == pid:
                        player = p
                        found_n = n
                        last = pur
                        break
                if last is not None:
                    break
            if last is not None:
                break
        if last is None:
            return "Покупка №" + str(pid) + " не найдена (возможно, она уже откачена)."

        purchases = player.get("purchases") or []
        purchases.remove(last)
        player["purchases"] = purchases
        kind = last.get("kind")
        if kind == "vacation":
            # сбрасываем отпуск только если это самая свежая покупка отпуска
            later = any(
                x.get("kind") == "vacation" and x.get("id", 0) > pid
                for x in purchases
            )
            if not later:
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
        user_text = (
            "Ваша покупка №" + str(pid) + " «" + item_name + "» отменена администратором. "
            "Баллы возвращены: +" + str(price) + "."
        )
        result_text = "баллы возвращены (+" + str(price) + ")"
    else:
        user_text = "Ваша покупка №" + str(pid) + " «" + item_name + "» отменена администратором. Баллы не возвращены."
        result_text = "баллы не возвращены"

    admin_text = (
        "Покупка №" + str(pid) + " «" + item_name + "» у " + nick + " (" + LIST_TITLE[found_n] + ") откачена, "
        + result_text + ". Сейчас у него " + str(points) + " баллов."
    )
    all_admins_text = (
        "↩️ Админ [id" + str(admin_id) + "|" + admin_name + "] откатил покупку №" + str(pid)
        + " «" + item_name + "» у " + nick + " (" + LIST_TITLE[found_n] + "), " + result_text + "."
    )
    executor.submit(send_to_user, vk_id, user_text)
    notify_rollback(found_n, all_admins_text, admin_id)
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
            _, ch2 = otgul_active(p)
            changed = changed or ch2
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
            otgul_end = p.get("otgul_end")
            link = "(vk.com/id" + str(p["vk_id"]) + ")"
            if vacation:
                status = "отпуск до " + str(vacation)
            elif otgul_end:
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
# /кто — кому не доходят сообщения бота
# (привязан к таблице, но не писал боту / запретил сообщения)
# ==========================================================
def cmd_who(peer_id, is_common):
    lists = (1, 2) if is_common else (2,)
    with lock:
        by_list = {}
        for n in lists:
            by_list[n] = sorted(
                [(p["nick"], p["vk_id"]) for p in P(n).values()],
                key=lambda t: t[0].lower(),
            )
    all_ids = list(dict.fromkeys(vid for n in lists for _, vid in by_list[n]))
    if not all_ids:
        return "Нет привязанных игроков."

    reply(peer_id, "🔎 Проверяю " + str(len(all_ids)) + " игроков, подождите...")

    allowed = {}  # vk_id -> True / False / None (не удалось проверить)
    for uid in all_ids:
        try:
            res = vk.messages.isMessagesFromGroupAllowed(group_id=GROUP_ID, user_id=uid)
            allowed[uid] = bool(res.get("is_allowed")) if isinstance(res, dict) else bool(res)
        except Exception as e:
            print("/кто: не удалось проверить " + str(uid) + ":", repr(e))
            allowed[uid] = None
        time.sleep(0.07)

    out = []
    total_bad = 0
    total_unknown = 0
    for n in lists:
        bad = [(nick, vid) for nick, vid in by_list[n] if allowed.get(vid) is False]
        unknown = [(nick, vid) for nick, vid in by_list[n] if allowed.get(vid) is None]
        total_bad += len(bad)
        total_unknown += len(unknown)
        if not bad and not unknown:
            continue
        out.append("📋 " + LIST_TITLE[n] + ":")
        for i, (nick, vid) in enumerate(bad, 1):
            out.append(str(i) + ". " + nick + " — vk.com/id" + str(vid))
        if unknown:
            out.append("⚠️ Не удалось проверить:")
            for nick, vid in unknown:
                out.append("• " + nick + " — vk.com/id" + str(vid))
        out.append("")

    if not out:
        return "✅ Всем привязанным игрокам сообщения доходят."
    header = (
        "❌ Сообщения не доходят: " + str(total_bad) + " чел.\n"
        "(привязаны, но не писали боту или запретили сообщения — "
        "рассылки и уведомления им не приходят)\n"
    )
    if total_unknown:
        header += "Не удалось проверить: " + str(total_unknown) + "\n"
    return header + "\n" + "\n".join(out).rstrip()
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
# /написать — обращение последнему писавшему админу (или владельцу)
# ==========================================================
def _last_admin_for(from_id):
    """Возвращает ID админа, который последний писал этому игроку, или OWNER_ID."""
    with lock:
        last = (data.get("last_admin_msg") or {}).get(str(from_id))
    if last and (last in ALLOWED_IDS or last in NADMIN_IDS):
        return last
    return OWNER_ID
def cmd_write(msg, from_id, text):
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return "Формат: /написать текст"
    body_text = parts[1].strip()
    target_admin = _last_admin_for(from_id)
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
                user_id=target_admin,
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
                user_id=target_admin,
                message=header + ":\n\n" + body_text,
                random_id=get_random_id(),
                keyboard=keyboard,
            )
        except Exception as e:
            print("Ошибка отправки обращения:", repr(e))
            return "Не удалось отправить сообщение. Попробуйте позже."
    return "✅ Ваше сообщение отправлено."
# ==========================================================
# /ответить — ответ админа на обращение (/написать)
# Использование: ответить (reply) на сообщение бота «📩 Сообщение от ...»
# и написать /ответить текст. Пользователь получит:
# «Ответ от администратора: текст»
# ==========================================================
def cmd_reply_to_user(msg, from_id, text):
    if from_id not in ALLOWED_IDS and from_id not in NADMIN_IDS:
        return None
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return "Формат: ответьте на сообщение бота командой /ответить текст"
    answer_text = parts[1].strip()

    replied = msg.get("reply_message")
    if not replied:
        return "Ответьте (reply) на сообщение бота с обращением и напишите /ответить текст"
    if replied.get("from_id") != -GROUP_ID:
        return "Нужно отвечать именно на сообщение от бота с обращением."

    target_id = None
    match = re.search(r"\[id(\d+)\|", replied.get("text") or "")
    if match:
        target_id = int(match.group(1))
    if target_id is None:
        fwd = replied.get("fwd_messages") or []
        if fwd and isinstance(fwd[0], dict) and fwd[0].get("from_id", 0) > 0:
            target_id = fwd[0]["from_id"]
    if target_id is None:
        return "Не удалось определить автора обращения. Отвечайте на сообщение «📩 Сообщение от ...»."

    try:
        vk.messages.send(
            user_id=target_id,
            message="Ответ от администратора: " + answer_text,
            random_id=get_random_id(),
        )
    except Exception as e:
        print("Не удалось отправить ответ:", repr(e))
        return "❌ Не удалось отправить ответ пользователю."
    with lock:
        data.setdefault("last_admin_msg", {})[str(target_id)] = from_id
        save_data()
    return "✅ Ответ отправлен [id" + str(target_id) + "|пользователю]."
# ==========================================================
# ОТЧЁТЫ: общие функции
# ==========================================================
REPORT_DATE_RE = r"(\d{2})\.(\d{2})\.(\d{4})"
def parse_report_date(text):
    """'дд.мм.гггг' -> date. Возвращает datetime.date или None."""
    match = re.fullmatch(REPORT_DATE_RE, text.strip())
    if not match:
        return None
    try:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    except ValueError:
        return None
def format_report_date(d):
    return d.strftime("%d.%m.%Y")
def chunk_lines(lines, limit=3500):
    """Группирует строки в сообщения до limit символов."""
    chunks = []
    cur = []
    size = 0
    for line in lines:
        add = len(line) + 1
        if cur and size + add > limit:
            chunks.append("\n".join(cur))
            cur = []
            size = 0
        cur.append(line)
        size += add
    if cur:
        chunks.append("\n".join(cur))
    return chunks
def plural_screens(k):
    if k % 10 == 1 and k % 100 != 11:
        return "скрин"
    if 2 <= k % 10 <= 4 and not (12 <= k % 100 <= 14):
        return "скрина"
    return "скринов"
def extract_token(raw):
    """Вычищает токен из любого текста: переносы строк, пробелы, кавычки, слова вокруг."""
    raw = raw or ""
    PAT = r"vk1\.[a-zA-Z]\.[A-Za-z0-9_\-]{10,}"
    # токен одной строкой: если он достаточно длинный — он полный, брать его целиком
    # (иначе к хвосту приклеился бы текст, написанный следом латиницей)
    full = [m.group(0).rstrip(".") for m in re.finditer(PAT, raw)]
    full = [t for t in full if len(t) >= 40]
    if full:
        return max(full, key=len)
    # короче 40 символов — вероятно токен разбили переносом/пробелом, склеиваем и ищем
    glued = re.sub(r"\s+", "", raw)
    found = [m.group(0) for m in re.finditer(PAT, glued)]
    found += full
    if found:
        return max(found, key=len)
    # старый/необычный ключ: самый длинный кусок из допустимых символов
    cands = re.findall(r"[A-Za-z0-9_\-]{15,}", glued)
    if cands:
        return max(cands, key=len)
    return ""
def extract_group_id(raw):
    """Достаёт ID группы из текста, если пользователь его дописал. Иначе 0."""
    if not raw:
        return 0
    m = re.search(r"(?:^|\D)(-?\d{5,12})(?!\d)", raw)
    return abs(int(m.group(1))) if m else 0
def cmd_report_connect(user_id, raw_text, group_id=""):
    """Команда +токен: подключить страничного бота.
    Принимает СЫРОЙ текст после команды: бот сам вытаскивает токен и проверяет его."""
    global PAGE_TOKEN, PAGE_VK, PAGE_GROUP_ID, PAGE_READY
    # извлекаем токен и (опционально) ID группы из всего, что прислал человек
    token = extract_token(raw_text)
    if not token:
        return (
            "❌ Не нашёл токен в сообщении.\n"
            "Напиши так (токен можно вставить даже с переносами и мусором вокруг):\n"
            "+токен vk1.a.XXXXXXXXXXXX"
        )
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{10,512}", token):
        return "❌ Токен выглядит некорректно: " + token[:20] + "..."
    # Проверяем токен через users.get() — это работает только с токеном пользователя
    token_owner_id = 0
    token_owner_name = ""
    try:
        test_vk = vk_api.VkApi(token=token)
        test_api = test_vk.get_api()
        # Для user-токена users.get() без параметров возвращает владельца токена
        try:
            me = test_api.users.get()
            if me:
                token_owner_id = int(me[0].get("id"))
                token_owner_name = (me[0].get("first_name", "") + " " + me[0].get("last_name", "")).strip()
        except Exception:
            pass
        # Если всё ещё не получили id — возможно это токен сообщества
        if not token_owner_id:
            try:
                info = test_api.groups.getById()
                if isinstance(info, dict):
                    info = info.get("groups") or []
                if info:
                    return (
                        "❌ Это токен сообщества, а нужен токен пользователя.\n"
                        "Токен пользователя получается через vk.com/dev → «Создать токен» (с правами messages).\n"
                        "Или: Управление сообществом → Настройки → Работа с API → Ключи API → Ключ пользователя"
                    )
            except Exception:
                pass
            return "❌ Не удалось проверить токен — возможно он неверный или просрочен."
    except vk_api.exceptions.ApiError as e:
        code = getattr(e, "code", "?")
        hint = ""
        if code == 5:
            hint = "\nТокен устарел или отозван — создайте новый."
        elif code == 9:
            hint = "\nТокен не найден — скопируйте его целиком (начинается с vk1.a.)."
        elif code == 15:
            hint = "\nЭтот токен не имеет прав на работу с сообщениями. Нужен токен с правом messages."
        return "❌ VK отверг этот токен (код " + str(code) + ")." + hint
    except Exception as e:
        return "❌ Не удалось подключиться: " + repr(e)
    # Сохраняем: страничный бот — это пользователь, чей токен
    page_user_id = token_owner_id
    with lock:
        data["page"] = {
            "token": token,
            "user_id": page_user_id,
            "group_id": 0,   # для user-токена группа не нужна
        }
        save_data()
    PAGE_TOKEN = token
    PAGE_VK = test_vk
    PAGE_GROUP_ID = 0
    PAGE_READY = True
    # запускаем «живого» страничного бота (слушает ЛС с сообществом)
    start_page_bot()
    st = get_report_settings()
    extra = ""
    if not st["group_id"] or not st["topic_ot"] or not st["topic_otchety"]:
        extra = (
            "\n\nНе задано место проверки отчётов:\n"
            + ("/отгруппа <id> — сообщество\n" if not st["group_id"] else "")
            + ("/отобс от <id> — обсуждение для /от\n" if not st["topic_ot"] else "")
            + ("/отобс отчеты <id> — обсуждение для /отчеты" if not st["topic_otchety"] else "")
        )
    return (
        "✅ Страничный бот подключён и запущен.\n"
        "Пользователь: " + str(page_user_id) + (" («" + token_owner_name + "»)" if token_owner_name else "") + "\n"
        "Токен: " + token[:8] + "…" + token[-6:] + "\n"
        "Сохранён навсегда — при перезапуске подхватится сам." + extra
    )
def cmd_report_disconnect(user_id):
    """Команда -токен: отключить страничный бот."""
    global PAGE_TOKEN, PAGE_VK, PAGE_GROUP_ID, PAGE_READY
    with lock:
        if not data.get("page", {}).get("token"):
            return "Страничный бот не подключён."
        data["page"] = {"token": "", "user_id": 0, "group_id": 0}
        save_data()
    stop_page_bot()
    PAGE_TOKEN = None
    PAGE_VK = None
    PAGE_GROUP_ID = None
    PAGE_READY = False
    return "✅ Страничный бот отключён."
# ==========================================================
# ОТЧЁТЫ: сторона ОСНОВНОГО бота
#   1. админ пишет в чате /отчеты дд.мм.гггг (или /от дд.мм.гггг)
#   2. основной бот пишет страничному боту «/отчеты дд.мм.гггг»
#   3. страничный бот отвечает «/список» (или «/нлист») — основной бот присылает ники
#   4. страничный бот проверяет обсуждение, пишет там «все выше отчеты проверены»
#      и присылает основному боту результаты
#   5. основной бот публикует результаты в чат, где была введена команда
# ==========================================================
def cmd_reports_start(date_str, n, peer_id):
    """Начать проверку отчётов. n=1 для /отчеты, n=2 для /от."""
    if not is_page_connected():
        return "❌ Страничный бот не подключён. Напиши в ЛС: +токен vk1.a.XXXXX"
    target_date = parse_report_date(date_str)
    if target_date is None:
        return "Формат: " + ("/отчеты дд.мм.гггг" if n == 1 else "/от дд.мм.гггг")
    user_id = get_page_user_id()
    if not user_id:
        return "❌ Не удалось определить ID пользователя страничного бота."
    with lock:
        if not P(n):
            return "Список пуст, проверять нечего."
        now = time.time()
        # чистим протухшие проверки и не даём запускать две сразу
        for key, st in list(PAGE_STATE.items()):
            if now - st.get("ts", 0) > PAGE_TIMEOUT:
                del PAGE_STATE[key]
        if PAGE_STATE:
            return "⏳ Проверка отчётов уже идёт, подождите её окончания."
        # новый запуск проверки отменяет прежние вопросы «выдать баллы / рассылку» в этом чате
        old_ask = REPORT_AWAIT.pop(peer_id, None)
        PAGE_STATE[peer_id] = {
            "stage": "waiting_list",
            "n": n,
            "date": date_str,
            "target_date": target_date,
            "peer_id": peer_id,
            "user_id": user_id,
            "ts": now,
            "parts": {},
            "total": 0,
        }
    if old_ask:
        close_question(peer_id, old_ask, "⏭ Вопрос отменён: запущена новая проверка.")
    # убеждаемся, что страничный бот слушает
    if not (PAGE_THREAD and PAGE_THREAD.is_alive()):
        start_page_bot()
        time.sleep(3)
    cmd = "/от" if n == 2 else "/отчеты"
    if not send_to_page(user_id, cmd + " " + date_str):
        with lock:
            PAGE_STATE.pop(peer_id, None)
        return (
            "❌ Не удалось написать страничному боту. Пусть он один раз напишет сообществу "
            "(например /меню) и убедитесь, что сообщения сообщества включены."
        )
    return "✅ Проверка отчётов запущена для " + date_str + " (" + ("списка /нлист" if n == 2 else "списка /список") + "). Ждите результат."
def _find_page_state(user_id):
    best_peer = None
    best = None
    for peer, st in PAGE_STATE.items():
        if st.get("user_id") != user_id:
            continue
        if best is None or st.get("ts", 0) > best.get("ts", 0):
            best_peer, best = peer, st
    return best_peer, best
def _page_status_of(player):
    """Статус игрока для страничного бота: работает / отгул / отпуск до ..."""
    active, _ = vacation_active(player)
    if active:
        return "отпуск до " + str(player.get("vacation"))
    otgul_on, _ = otgul_active(player)
    if otgul_on:
        return "отгул"
    return "работает"
def handle_page_dialog(from_id, text):
    """Сообщение от страничного бота основному. True — сообщение обработано."""
    text = (text or "").strip()
    if not text:
        return False
    with lock:
        peer_id, st = _find_page_state(from_id)
    if st is None:
        return False
    if time.time() - st.get("ts", 0) > PAGE_TIMEOUT:
        with lock:
            PAGE_STATE.pop(peer_id, None)
        return False
    head = text.split(maxsplit=1)[0].lower()
    stage = st.get("stage")

    # Шаг 3: страничный бот запросил список -> шлём ники (и статусы)
    if stage == "waiting_list":
        if head not in ("/список", "/списко", "/нлист", "/nlist"):
            return False
        n = st["n"]
        lines = []
        changed = False
        with lock:
            for p in sorted(P(n).values(), key=lambda x: x["nick"].lower()):
                _, ch = vacation_active(p)
                changed = changed or ch
                _, ch2 = otgul_active(p)
                changed = changed or ch2
                lines.append(p["nick"] + " | " + str(p["vk_id"]) + " | " + _page_status_of(p))
            if changed:
                save_data()
            st["stage"] = "waiting_results"
            st["ts"] = time.time()
        chunks = chunk_lines(lines) or [""]
        total = len(chunks)
        for i, body in enumerate(chunks, 1):
            send_to_page(from_id, "НИКИ " + str(i) + "/" + str(total) + "\n" + body)
            time.sleep(0.3)
        return True

    # Шаг 5: результаты проверки
    if stage == "waiting_results":
        if text.startswith("ОШИБКА"):
            with lock:
                PAGE_STATE.pop(peer_id, None)
            reply(peer_id, "❌ Страничный бот: " + text[len("ОШИБКА"):].strip(": \n"))
            return True
        m = re.match(r"РЕЗУЛЬТАТЫ\s+(\d+)/(\d+)[ \t]*\n?(.*)", text, re.S)
        if m:
            idx = int(m.group(1))
            total = int(m.group(2))
            body = m.group(3).strip("\n")
            with lock:
                st["parts"][idx] = body
                st["total"] = total
                st["ts"] = time.time()
                ready = len(st["parts"]) >= total
            if ready:
                finalize_page_results(peer_id, st)
            return True
        if text.startswith("/"):
            return False
        return True   # «ники получены, проверяю отчеты» и прочее — игнорируем
    return False
def _report_shots_count(status):
    """'есть отчет 14 скринов' -> 14. Другой статус (нет отчета/отгул/отпуск) -> None."""
    m = re.search(r"есть\s+отч[её]т\s+(\d+)", (status or "").lower())
    return int(m.group(1)) if m else None
def _shot_status_display(status):
    """Как показывать статус игрока. <REPORT_MIN_SHOTS -> не засчитан. 4-5 -> на проверке."""
    k = _report_shots_count(status)
    if k is not None and k < REPORT_MIN_SHOTS:
        return (
            "не засчитан (мало скринов: " + str(k) + " " + plural_screens(k)
            + ", нужно от " + str(REPORT_MIN_SHOTS) + ")"
        )
    if k is not None and k in REPORT_CHECK_SHOTS:
        return status + " 🔎 на проверке"
    return status
def _points_for_shots(k):
    """Норма: < REPORT_MIN_SHOTS скринов -> 0; >= REPORT_FULL_SHOTS -> полный балл, иначе частичный. Без отчёта -> 0."""
    if not k or k < REPORT_MIN_SHOTS:
        return 0
    return REPORT_FULL_POINTS if k >= REPORT_FULL_SHOTS else REPORT_PART_POINTS
def yes_no_keyboard(action):
    """Инлайн-клавиатура «Да / Нет» — кнопки внутри сообщения бота."""
    kb = VkKeyboard(inline=True)
    btn(kb, "✅ Да", "rep_answer", VkKeyboardColor.POSITIVE, a=action, v="yes")
    btn(kb, "❌ Нет", "rep_answer", VkKeyboardColor.NEGATIVE, a=action, v="no")
    return kb.get_keyboard()
def close_question(peer_id, st, note):
    """Редактирует сообщение с вопросом: дописывает ответ и убирает кнопки,
    чтобы нажать повторно было нельзя. True — сообщение обновлено."""
    if not st or not st.get("msg_id"):
        return False
    base = str(st.get("question") or "").strip()
    text = (base + "\n\n" if base else "") + note
    if len(text) > 4000:
        text = text[:4000]
    return edit_message(peer_id, st["msg_id"], text)
def hide_keyboard():
    """Пустой набор кнопок: VK по нему убирает обычное меню под полем ввода."""
    return json.dumps({"one_time": True, "buttons": []}, ensure_ascii=False)
def hide_inline_keyboard():
    """Пустой набор inline-кнопок: убирает кнопки внутри сообщения."""
    return json.dumps({"inline": True, "buttons": []}, ensure_ascii=False)
def close_chat_questions(peer_id, note="⏹ Кнопки убраны (команда /help)."):
    """Убирает активный вопрос с кнопками в этом чате (редактирует сообщение).
    Возвращает True — если вопрос был и кнопки сняты."""
    with lock:
        st = REPORT_AWAIT.pop(peer_id, None)
    if st is None:
        return False
    close_question(peer_id, st, note)
    return True
def _cleanup_report_await():
    """Убирает протухшие вопросы по чатам."""
    now = time.time()
    expired = []
    for peer in list(REPORT_AWAIT.keys()):
        if now - REPORT_AWAIT[peer].get("ts", 0) > REPORT_AWAIT_TIMEOUT:
            expired.append((peer, REPORT_AWAIT.pop(peer, None)))
    for peer, st in expired:
        close_question(peer, st, "⌛ Вопрос устарел — ответ не принят.")
def _report_state(peer_id):
    """Текущий вопрос по этому чату (или None, если его нет/он устарел)."""
    _cleanup_report_await()
    with lock:
        st = REPORT_AWAIT.get(peer_id)
        if st is not None:
            st["ts"] = time.time()
    return st
def finalize_page_results(peer_id, st):
    """Публикует результаты в чат, сохраняет их и спрашивает, выдавать ли баллы."""
    n = st.get("n", 2)
    date_str = st.get("date", "")
    parts = st.get("parts", {})
    body = "\n".join(parts[i] for i in sorted(parts))
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    with lock:
        prev = ((data.get("reports") or {}).get(date_str) or {}).get(str(n)) or {}
    saved = {}
    out_lines = []
    for ln in lines:
        nick, _, status = ln.partition(" ")
        nick = nick.strip()
        status = status.strip()
        if not nick:
            continue
        old_status = prev.get(nick)
        if status.lower() == "без изменений":
            # повторная проверка за тот же день: после метки от игрока ничего нет
            if old_status:
                status = old_status
                out_lines.append(nick + " " + _shot_status_display(status) + " (уже проверен)")
            else:
                status = "нет отчета"
                out_lines.append(nick + " " + status)
        else:
            old_k = _report_shots_count(old_status)
            new_k = _report_shots_count(status)
            if old_k and new_k:
                total_k = old_k + new_k
                status = "есть отчет " + str(total_k) + " " + plural_screens(total_k)
                out_lines.append(nick + " " + _shot_status_display(status) + " (+" + str(new_k) + " после прошлой проверки)")
            else:
                out_lines.append(nick + " " + _shot_status_display(status))
        saved[nick] = status
    with lock:
        PAGE_STATE.pop(peer_id, None)
        if saved:
            data.setdefault("reports", {}).setdefault(date_str, {})[str(n)] = saved
            save_data()
    out = "📋 Отчёты за " + date_str + " (" + LIST_TITLE[n] + "):\n" + ("\n".join(out_lines) if out_lines else "пусто")
    reply_long(peer_id, out)
    # считаем, кому положены баллы (повторно не выдаём тем, кому уже начисляли)
    with lock:
        already_map = ((data.get("points_awarded") or {}).get(date_str) or {}).get(str(n)) or {}
    plan = []
    skipped_awarded = []
    for nick, status in saved.items():
        k = _report_shots_count(status)
        pts = _points_for_shots(k)
        if pts <= 0:
            continue
        given = already_map.get(nick)
        if nick in already_map and given is None:
            # старый формат записи (сумма неизвестна) — повторно не выдаём
            skipped_awarded.append(nick)
            continue
        given = int(given or 0)
        delta = pts - given
        if delta <= 0:
            skipped_awarded.append(nick)
            continue
        plan.append({
            "nick": nick, "delta": delta, "norm": pts, "shots": k,
            "check": k in REPORT_CHECK_SHOTS,
        })
    auto = [i for i in plan if not i["check"]]
    review = [i for i in plan if i["check"]]
    if not auto:
        # выдавать сразу нечего — сразу начинаем цепочку проверки (4 скрина)
        if not review and skipped_awarded:
            reply(peer_id, "✅ Баллы за " + date_str + " уже были выданы: " + ", ".join(skipped_awarded) + ".")
        elif not review:
            reply(peer_id, "💭 Принятых отчётов нет — выдавать баллы некому.")
        else:
            with lock:
                REPORT_AWAIT.pop(peer_id, None)
            _begin_review(peer_id, {"n": n, "date": date_str, "plan": [], "review": review, "awarded": []})
        return
    lines_q = ["💰 Выдать баллы за отчёты за " + date_str + " (" + LIST_TITLE[n] + ")?"]
    for item in auto:
        given = int(already_map.get(item["nick"]) or 0)
        note = ""
        if given > 0:
            note = " (доначисление: норма " + str(item["norm"]) + " − уже " + str(given) + ")"
        lines_q.append("• " + item["nick"] + " — +" + str(item["delta"]) + note)
    lines_q.append("")
    lines_q.append(
        "Итого: " + str(len(auto)) + " игрокам, " + str(sum(i["delta"] for i in auto)) + " баллов."
    )
    if review:
        lines_q.append("")
        lines_q.append(
            "🔎 У " + ", ".join(i["nick"] for i in review) + " по "
            + str(min(REPORT_CHECK_SHOTS)) + "-" + str(max(REPORT_CHECK_SHOTS)) + " "
            + plural_screens(max(REPORT_CHECK_SHOTS))
            + " — перед выдачей покажу их скрины и спрошу подтверждение отдельно."
        )
    q_text = "\n".join(lines_q)
    q_id = reply(peer_id, q_text, yes_no_keyboard("points"))
    with lock:
        REPORT_AWAIT[peer_id] = {
            "stage": "ask_points", "n": n, "date": date_str,
            "plan": auto, "review": review, "awarded": [], "ts": time.time(),
            "msg_id": q_id, "question": q_text,
        }
def _grant_one(n, date_str, item):
    """Выдаёт баллы одному игроку. Возвращает (awarded|None, missed_nick|None)."""
    with lock:
        nick = str(item.get("nick") or "")
        delta = int(item.get("delta", 0) or 0)
        norm = int(item.get("norm", 0) or 0)
        player = find_by_nick(n, nick) if nick else None
        if player is None or delta <= 0:
            return None, (nick or None)
        player["points"] += delta
        contest_record(n, player, delta)
        awarded = {
            "nick": player["nick"], "vk_id": player["vk_id"],
            "pts": delta, "norm": norm, "total": player["points"],
        }
        # запоминаем суммарно выданную за эту дату норму, чтобы не выдать дважды
        awarded_store = data.setdefault("points_awarded", {})
        day = awarded_store.setdefault(date_str, {})
        already = day.setdefault(str(n), {})
        prev = already.get(awarded["nick"])
        prev = int(prev) if isinstance(prev, int) else 0
        already[awarded["nick"]] = max(prev, norm)
        save_data()
        return awarded, None
def _ask_show_question(st):
    """Вопрос «показать скрины?» для первого из очереди на проверку (4-5 скринов)."""
    item = st["review"][0]
    q = (
        "🔎 У " + item["nick"] + " " + str(item["shots"]) + " " + plural_screens(item["shots"])
        + " (норма " + str(item["norm"]) + " баллов — выдам только после показа и подтверждения).\n"
        + "Показать его скрины?"
    )
    st["stage"] = "ask_show"
    st["question"] = q
    return q, yes_no_keyboard("show")
def _begin_review(peer_id, base):
    """Запускает цепочку «показать скрины -> выдать?» без основного вопроса."""
    base["ts"] = time.time()
    base["plan"] = []
    q, kb = _ask_show_question(base)
    q_id = reply(peer_id, q, kb)
    base["msg_id"] = q_id
    with lock:
        REPORT_AWAIT[peer_id] = base
def _advance_review(peer_id, st):
    """Следующий шаг цепочки проверки: новый вопрос, рассылка или завершение."""
    st["msg_id"] = None
    if st.get("review"):
        q, kb = _ask_show_question(st)
    elif st.get("awarded") or st.get("rejected"):
        q, kb = _broadcast_question(st)
    else:
        with lock:
            REPORT_AWAIT.pop(peer_id, None)
        return "✅ Проверка завершена — баллов не выдавали.", None
    with lock:
        st["ts"] = time.time()
        REPORT_AWAIT[peer_id] = st
    return q, kb
def _broadcast_question(st):
    """Вопрос про рассылку (после всех выдач)."""
    awarded = st.get("awarded") or []
    rejected = st.get("rejected") or []
    date_str = st.get("date", "")
    n = st.get("n", 2)
    lines = []
    if awarded:
        lines.append("💰 Выдал баллы за отчёты за " + date_str + " (" + LIST_TITLE[n] + "):")
        for a in awarded:
            is_bonus = a["pts"] < a["norm"]
            tag = " (доначислено)" if is_bonus else ""
            lines.append("• " + a["nick"] + " +" + str(a["pts"]) + " (теперь " + str(a["total"]) + ")" + tag)
    if rejected:
        lines.append("❌ Не выдано баллов (отклонено): " + ", ".join(rejected))
    lines.append("")
    lines.append("📣 Делаем рассылку (всем: кому выдали и кому не выдали баллы)?")
    q_text = "\n".join(lines)
    st["stage"] = "ask_broadcast"
    st["question"] = q_text
    return q_text, yes_no_keyboard("broadcast")
def _request_review_shots(peer_id, st):
    """Просит страничный бот прислать скрины проверяемого игрока. Возвращает (текст, клавиатура)."""
    item = st["review"][0]
    n = st.get("n", 2)
    page_uid = get_page_user_id()
    if not page_uid or not is_page_connected():
        with lock:
            REPORT_AWAIT.pop(peer_id, None)
        return "❌ Страничный бот не подключён — скрины показать не могу.", None
    with lock:
        player = find_by_nick(n, item["nick"])
        for key, s in list(SHOTS_STATE.items()):
            if time.time() - s.get("ts", 0) > SHOTS_TIMEOUT:
                del SHOTS_STATE[key]
        if SHOTS_STATE:
            with lock:
                REPORT_AWAIT.pop(peer_id, None)
            return "⏳ Сейчас идёт другой сбор скринов — проверка остановлена. Дождитесь и запустите /отчеты заново.", None
        if player is None:
            st["review"].pop(0)
            note = "⚠️ Игрок " + item["nick"] + " не найден в списке " + LIST_TITLE[n] + " — пропускаю."
            q, kb = _advance_review(peer_id, st)
            return note + "\n\n" + q, kb
        SHOTS_STATE[peer_id] = {
            "user_id": page_uid, "peer_id": peer_id, "nick": item["nick"],
            "vk_id": player["vk_id"], "lists": [n], "ts": time.time(),
            "parts": {}, "total": 0, "after": "review",
        }
        st["stage"] = "waiting_shots"
        st["msg_id"] = None
        st["ts"] = time.time()
        REPORT_AWAIT[peer_id] = st
    if not send_to_page(page_uid, "/скрины " + str(player["vk_id"]) + " " + str(n)):
        with lock:
            SHOTS_STATE.pop(peer_id, None)
            REPORT_AWAIT.pop(peer_id, None)
        return "❌ Не удалось написать страничному боту — проверка остановлена.", None
    return "🔎 Ищу скрины " + item["nick"] + ", ждите...", None
def _review_shots_done(peer_id, note):
    """Скрины показаны (или не найдены) — спрашиваем, выдавать ли баллы этому игроку."""
    with lock:
        st = REPORT_AWAIT.get(peer_id)
        if st is None or st.get("stage") != "waiting_shots" or not st.get("review"):
            return
        item = st["review"][0]
        q = (
            note + "\n\n💰 Выдать " + item["nick"] + " " + str(item["delta"])
            + " баллов за отчёт за " + st.get("date", "") + " (" + LIST_TITLE[st.get("n", 2)] + ")?"
        )
        st["stage"] = "ask_give"
        st["question"] = q
        st["ts"] = time.time()
        q_id = reply(peer_id, q, yes_no_keyboard("give"))
        st["msg_id"] = q_id
def _review_give(peer_id, st, approve):
    """Итог вопроса «выдать баллы проверяемому игроку?» и переход к следующему шагу."""
    item = st["review"].pop(0)
    if not approve:
        st["rejected"] = (st.get("rejected") or []) + [item["nick"]]
        note = "❌ Ок, " + item["nick"] + " баллы не выдаю."
    else:
        awarded, missed = _grant_one(st.get("n", 2), st.get("date", ""), item)
        if awarded is None:
            note = "⚠️ Не удалось выдать баллы: игрок " + str(missed or item["nick"]) + " не найден."
        else:
            st["awarded"] = (st.get("awarded") or []) + [awarded]
            note = ("✅ Выдал " + awarded["nick"] + " +" + str(awarded["pts"])
                    + " баллов (теперь " + str(awarded["total"]) + ").")
    q, kb = _advance_review(peer_id, st)
    return note + "\n\n" + q, kb
def award_report_points(peer_id, st):
    """Выдаёт баллы по основному плану. Дальше — цепочка проверки (4-5 скринов) или рассылка."""
    n = st.get("n", 2)
    date_str = st.get("date", "")
    plan = st.get("plan") or []
    awarded = []
    missed = []
    for item in plan:
        a, m = _grant_one(n, date_str, item)
        if a is not None:
            awarded.append(a)
        elif m:
            missed.append(m)
    st["awarded"] = (st.get("awarded") or []) + awarded
    lines = []
    if awarded:
        lines.append("💰 Выдал баллы за отчёты за " + date_str + " (" + LIST_TITLE[n] + "):")
        for a in awarded:
            is_bonus = a["pts"] < a["norm"]
            tag = " (доначислено)" if is_bonus else ""
            lines.append("• " + a["nick"] + " +" + str(a["pts"]) + " (теперь " + str(a["total"]) + ")" + tag)
    if missed:
        lines.append("⚠️ Не найден: " + ", ".join(missed))
    if st.get("review"):
        # сначала проверка игроков с 4-5 скринами, рассылка — после неё
        if not lines:
            lines.append("💭 По основному плану выдавать нечего — перехожу к проверке 4-5 скринов.")
        lines.append("")
        q, kb = _ask_show_question(st)
        return "\n".join(lines) + "\n" + q, kb
    if not st.get("awarded"):
        with lock:
            REPORT_AWAIT.pop(peer_id, None)
        text = "\n".join(lines) if lines else "❌ Не удалось выдать баллы: никто из списка не найден."
        return text, None
    return _broadcast_question(st)
def broadcast_report_points(peer_id, st):
    """Пишет каждому награждённому игроку в ЛС о начисленных баллах,
    а также тем кому баллы НЕ выданы — с указанием причины.
    Игрокам, которым баллы за эту дату УЖЕ были выданы в прошлую проверку
    (и про выдачу им уже писали), сообщение о невыдаче НЕ приходит.
    Повторное уведомление о выдаче возможно только как доначисление:
    отчёта не было -> игрок его залил -> бот пишет «доначислено»."""
    n = st.get("n", 2)
    date_str = st.get("date", "")
    awarded = st.get("awarded") or []
    rejected = st.get("rejected") or []
    with lock:
        REPORT_AWAIT.pop(peer_id, None)
        already_map = ((data.get("points_awarded") or {}).get(date_str) or {}).get(str(n)) or {}
    sent = 0
    failed = []
    for a in awarded:
        verb = "доначислено" if a["pts"] < a.get("norm", a["pts"]) else "начислено"
        text = (
            "✅ Вам " + verb + " " + str(a["pts"]) + " баллов за отчёт за " + date_str
            + " (" + LIST_TITLE[n] + ").\nСейчас у вас " + str(a["total"]) + " баллов."
        )
        try:
            vk.messages.send(user_id=a["vk_id"], message=text, random_id=get_random_id())
            sent += 1
        except Exception as e:
            print("Не удалось написать " + str(a["vk_id"]) + ":", repr(e))
            failed.append(a["nick"])
        time.sleep(0.1)
    lines = [
        "📣 Рассылка выполнена: " + str(sent) + " из " + str(len(awarded))
        + " игрокам отправлено сообщение о начислении баллов."
    ]
    if failed:
        lines.append("⚠️ Не дошло: " + ", ".join(failed))

    # Определяем, кому баллы НЕ выданы, и рассылаем причины
    with lock:
        all_reports = ((data.get("reports") or {}).get(date_str) or {}).get(str(n)) or {}
    awarded_nicks = set(a["nick"] for a in awarded)
    rejected_set = set(rejected)

    # Категории невыдачи
    no_report = []          # "вы не залили отчет"
    vacation_otgul = []     # "у вас отгул/отпуск"
    not_enough = []         # "Ваших скринов в отчете не достаточно для оценки и выдачи баллов"
    already_notified = []   # баллы уже выданы ранее — писать ничего не нужно

    for nick, status in all_reports.items():
        if nick in awarded_nicks:
            continue
        # Баллы за эту дату уже выдавались в прошлую проверку (соо о выдаче
        # уже приходило) -> не пишем «вам не начислены баллы» повторно.
        if nick in already_map:
            already_notified.append(nick)
            continue
        status_lower = (status or "").lower()
        k = _report_shots_count(status)
        if nick in rejected_set:
            # Отклонён админом при проверке 4-5 скринов
            not_enough.append(nick)
        elif "нет отч" in status_lower:
            no_report.append(nick)
        elif "отгул" in status_lower or "отпуск" in status_lower:
            vacation_otgul.append(nick)
        elif k is not None and k < REPORT_MIN_SHOTS:
            not_enough.append(nick)
        else:
            # Остальные случаи (например, "без изменений" без пред. статуса) — тоже нет отчёта
            no_report.append(nick)

    # Рассылаем каждому свою причину
    total_nr_sent = 0
    total_vo_sent = 0
    total_ne_sent = 0
    nr_failed = []
    vo_failed = []
    ne_failed = []

    for nick in no_report:
        player = find_by_nick(n, nick)
        if player is None:
            continue
        text = (
            "❌ Вам не начислены баллы за отчёт за " + date_str + " (" + LIST_TITLE[n] + ").\n"
            "Причина: вы не залили отчет."
        )
        try:
            vk.messages.send(user_id=player["vk_id"], message=text, random_id=get_random_id())
            total_nr_sent += 1
        except Exception as e:
            print("Не удалось написать " + str(player["vk_id"]) + ":", repr(e))
            nr_failed.append(nick)
        time.sleep(0.1)

    for nick in vacation_otgul:
        player = find_by_nick(n, nick)
        if player is None:
            continue
        text = (
            "ℹ️ Вам не начислены баллы за отчёт за " + date_str + " (" + LIST_TITLE[n] + ").\n"
            "Причина: у вас отгул/отпуск."
        )
        try:
            vk.messages.send(user_id=player["vk_id"], message=text, random_id=get_random_id())
            total_vo_sent += 1
        except Exception as e:
            print("Не удалось написать " + str(player["vk_id"]) + ":", repr(e))
            vo_failed.append(nick)
        time.sleep(0.1)

    for nick in not_enough:
        player = find_by_nick(n, nick)
        if player is None:
            continue
        text = (
            "⚠️ Вам не начислены баллы за отчёт за " + date_str + " (" + LIST_TITLE[n] + ").\n"
            "Причина: Ваших скринов в отчете не достаточно для оценки и выдачи баллов."
        )
        try:
            vk.messages.send(user_id=player["vk_id"], message=text, random_id=get_random_id())
            total_ne_sent += 1
        except Exception as e:
            print("Не удалось написать " + str(player["vk_id"]) + ":", repr(e))
            ne_failed.append(nick)
        time.sleep(0.1)

    total_not_awarded = len(no_report) + len(vacation_otgul) + len(not_enough)
    if total_not_awarded > 0:
        lines.append("")
        lines.append("📩 Сообщения о невыдаче баллов:")
        if no_report:
            lines.append("  • Не залили отчёт (" + str(total_nr_sent) + "/" + str(len(no_report)) + "): " + ", ".join(no_report))
        if vacation_otgul:
            lines.append("  • Отгул/отпуск (" + str(total_vo_sent) + "/" + str(len(vacation_otgul)) + "): " + ", ".join(vacation_otgul))
        if not_enough:
            lines.append("  • Мало скринов (" + str(total_ne_sent) + "/" + str(len(not_enough)) + "): " + ", ".join(not_enough))
        all_nr_failed = nr_failed + vo_failed + ne_failed
        if all_nr_failed:
            lines.append("⚠️ Не дошло (невыдача): " + ", ".join(all_nr_failed))
    if already_notified:
        lines.append("")
        lines.append("✅ Баллы уже были выданы ранее — не уведомляю повторно: " + ", ".join(already_notified))

    return "\n".join(lines), None
def report_answer_response(user_id, peer_id, payload):
    """Нажатие «Да/Нет» под вопросом бота по отчётам. Возвращает (текст, клавиатура) или None."""
    if user_id not in ALLOWED_IDS and user_id not in NADMIN_IDS:
        return None
    action = str(payload.get("a") or "")
    value = str(payload.get("v") or "")
    st = _report_state(peer_id)
    if st is None:
        return "Этот вопрос уже не актуален — запустите проверку заново (/от или /отчеты).", None
    stage = st.get("stage")
    need_stage = {"points": "ask_points", "broadcast": "ask_broadcast",
                  "show": "ask_show", "give": "ask_give"}.get(action)
    if need_stage is None or stage != need_stage:
        return "На этот вопрос уже ответили. Новых вопросов нет.", None
    if action == "show":
        if value == "yes":
            close_question(peer_id, st, "✅ Ответ: Да — показываю скрины.")
            return _request_review_shots(peer_id, st)
        item = st["review"].pop(0)
        st["rejected"] = (st.get("rejected") or []) + [item["nick"]]
        close_question(peer_id, st, "❌ Ответ: Нет — скрины не показываю, баллы не выдаю.")
        if st.get("review") or st.get("awarded"):
            q, kb = _advance_review(peer_id, st)
            return "❌ Ок, " + item["nick"] + " баллы не выдаю (скрины не смотрели).\n\n" + q, kb
        with lock:
            REPORT_AWAIT.pop(peer_id, None)
        return "❌ Ок, скрины не показываю, баллы не выдаю.", None
    if action == "give":
        close_question(peer_id, st, "✅ Ответ: Да — выдаю баллы." if value == "yes"
                       else "❌ Ответ: Нет — баллы не выдаю.")
        return _review_give(peer_id, st, value == "yes")
    if value == "no":
        with lock:
            REPORT_AWAIT.pop(peer_id, None)
        if action == "broadcast":
            if not close_question(peer_id, st, "❌ Ответ: Нет — рассылку не делаю."):
                return "❌ Ок, рассылку не делаю.", None
            return None
        if not close_question(peer_id, st, "❌ Ответ: Нет — баллы не выдаю."):
            return "❌ Ок, баллы не выдаю.", None
        return None
    if action == "points":
        close_question(peer_id, st, "✅ Ответ: Да — выдаю баллы.")
        return award_report_points(peer_id, st)
    if action == "broadcast":
        close_question(peer_id, st, "✅ Ответ: Да — делаю рассылку.")
        return broadcast_report_points(peer_id, st)
    return "Неизвестное действие.", None
# ==========================================================
# /скрины Nick_Name | @username — все скрины игрока за сегодня
#   1. админ пишет в чате /скрины Nick
#   2. основной бот пишет страничному боту «/скрины <vk_id> <списки>»
#   3. страничный бот собирает скрины игрока из обсуждений за сегодня (МСК)
#      и присылает их основному боту
#   4. основной бот пересылает их в чат, где была введена команда
# ==========================================================
SHOTS_STATE = {}      # peer_id -> состояние запроса
SHOTS_TIMEOUT = 150
def cmd_shots(args, user_id, peer_id, is_common):
    if not is_page_connected():
        return "❌ Страничный бот не подключён. Напиши в ЛС: +токен vk1.a.XXXXX"
    if len(args) != 1:
        return "Формат: /скрины Nick_Name или /скрины @username"
    page_uid = get_page_user_id()
    if not page_uid:
        return "❌ Не удалось определить ID пользователя страничного бота."
    target = args[0]
    allowed = (1, 2) if is_common else (2,)

    found = []
    with lock:
        for n in allowed:
            p = find_by_nick(n, target)
            if p:
                found.append((n, p))
    if not found:
        uid = resolve_user(target)
        if uid:
            with lock:
                for n in allowed:
                    p = find_by_id(n, uid)
                    if p:
                        found.append((n, p))
    if not found:
        return "Игрок " + target + " не найден."

    vk_id = found[0][1]["vk_id"]
    nick = found[0][1]["nick"]
    lists = [n for n, p in found if p["vk_id"] == vk_id]

    st = get_report_settings()
    if not st["group_id"]:
        return "⚠️ Не задано сообщество: /отгруппа <id>."
    ready = [n for n in lists if (st["topic_otchety"] if n == 1 else st["topic_ot"])]
    if not ready:
        return settings_warning(lists[0]) or "Не заданы обсуждения для проверки."
    lists = ready

    with lock:
        now = time.time()
        for key, s in list(SHOTS_STATE.items()):
            if now - s.get("ts", 0) > SHOTS_TIMEOUT:
                del SHOTS_STATE[key]
        if SHOTS_STATE:
            return "⏳ Сбор скринов уже идёт, подождите."
        SHOTS_STATE[peer_id] = {
            "user_id": page_uid, "peer_id": peer_id, "nick": nick,
            "vk_id": vk_id, "lists": lists, "ts": now, "parts": {}, "total": 0,
        }
    if not (PAGE_THREAD and PAGE_THREAD.is_alive()):
        start_page_bot()
        time.sleep(3)
    if not send_to_page(page_uid, "/скрины " + str(vk_id) + " " + ",".join(str(x) for x in lists)):
        with lock:
            SHOTS_STATE.pop(peer_id, None)
        return "❌ Не удалось написать страничному боту."
    return "🔎 Ищу скрины " + nick + " за сегодня, ждите..."
def _best_photo_url(ph):
    sizes = ph.get("sizes") or []
    if sizes:
        try:
            best = max(sizes, key=lambda s: (s.get("width", 0) or 0) * (s.get("height", 0) or 0))
            return best.get("url") or ""
        except Exception:
            pass
    return ""
def _extract_attachments(msg):
    """Достаёт [(attachment-строка, ссылка)] из сообщения: фото и картинки-документы."""
    result = []
    for a in msg.get("attachments") or []:
        t = a.get("type")
        if t == "photo":
            ph = a.get("photo") or {}
            s = "photo" + str(ph.get("owner_id")) + "_" + str(ph.get("id"))
            if ph.get("access_key"):
                s += "_" + str(ph["access_key"])
            result.append((s, _best_photo_url(ph)))
        elif t == "doc":
            dc = a.get("doc") or {}
            s = "doc" + str(dc.get("owner_id")) + "_" + str(dc.get("id"))
            if dc.get("access_key"):
                s += "_" + str(dc["access_key"])
            result.append((s, dc.get("url") or ""))
    return result
def _full_attachments(msg):
    """Вложения из события + из полной версии сообщения (в событии VK может отдать не всё)."""
    items = _extract_attachments(msg)
    cmid = msg.get("conversation_message_id")
    if cmid:
        try:
            res = vk.messages.getByConversationMessageId(
                peer_id=msg["peer_id"], conversation_message_ids=cmid)
            lst = res.get("items") if isinstance(res, dict) else res
            if lst:
                full = _extract_attachments(lst[0])
                if len(full) >= len(items):
                    items = full
        except Exception as e:
            print("Скрины: не удалось получить полное сообщение:", repr(e))
    return items
def _send_shots(peer_id, header, items):
    """Отправляет скрины пачками по 10; если пачка не прошла — по одному, остаток шлёт ссылками."""
    failed = []
    for i in range(0, len(items), 10):
        batch = items[i:i + 10]
        text = header if i == 0 else "…продолжение"
        try:
            vk.messages.send(peer_id=peer_id, message=text,
                             attachment=",".join(a for a, _ in batch), random_id=get_random_id())
        except Exception as e:
            print("Скрины: пачка не отправилась, шлю по одному:", repr(e))
            if i == 0:
                reply(peer_id, header)
            for att, url in batch:
                try:
                    vk.messages.send(peer_id=peer_id, message="", attachment=att, random_id=get_random_id())
                except Exception as e2:
                    print("Скрины: не отправилось " + att + ":", repr(e2))
                    failed.append((att, url))
                time.sleep(0.3)
        time.sleep(0.4)
    if failed:
        lines = ["⚠️ Не удалось прикрепить фото: " + str(len(failed)) + " шт."]
        for att, url in failed:
            lines.append(url or att)
        reply_long(peer_id, "\n".join(lines))
def handle_page_shots(msg):
    """Сообщение от страничного бота со скринами. True — обработано."""
    from_id = msg.get("from_id")
    text = (msg.get("text") or "").strip()
    with lock:
        peer_id, st = None, None
        for peer, s in SHOTS_STATE.items():
            if s.get("user_id") == from_id:
                if st is None or s.get("ts", 0) > st.get("ts", 0):
                    peer_id, st = peer, s
    if st is None:
        return False
    if time.time() - st.get("ts", 0) > SHOTS_TIMEOUT:
        with lock:
            SHOTS_STATE.pop(peer_id, None)
        return False

    if text.startswith("ОШИБКА"):
        with lock:
            SHOTS_STATE.pop(peer_id, None)
        err = text[len("ОШИБКА"):].strip(": \n")
        reply(peer_id, "❌ Страничный бот: " + err)
        if st.get("after") == "review":
            _review_shots_done(peer_id, "⚠️ Скрины показать не удалось (" + err + ").")
        return True

    if text.startswith("СКРИНЫ_НЕТ"):
        with lock:
            SHOTS_STATE.pop(peer_id, None)
        reply(peer_id, "📭 У " + st["nick"] + " нет скринов за сегодня (" + today_local().strftime("%d.%m.%Y") + ").")
        if st.get("after") == "review":
            _review_shots_done(peer_id, "📭 Скринов за сегодня не нашлось.")
        return True

    m = re.match(r"СКРИНЫ\s+(\d+)/(\d+)", text)
    if not m:
        return False
    idx, total = int(m.group(1)), int(m.group(2))
    items = _full_attachments(msg)
    print("Скрины: часть " + str(idx) + "/" + str(total) + ", вложений: " + str(len(items)))
    with lock:
        st["parts"][idx] = items
        st["total"] = total
        st["ts"] = time.time()
        ready = len(st["parts"]) >= total
    if ready:
        with lock:
            SHOTS_STATE.pop(peer_id, None)
        all_items = []
        seen = set()
        for i in sorted(st["parts"]):
            for att, url in st["parts"][i]:
                if att not in seen:
                    seen.add(att)
                    all_items.append((att, url))
        if not all_items:
            reply(peer_id, "📭 У " + st["nick"] + " нет скринов за сегодня.")
            if st.get("after") == "review":
                _review_shots_done(peer_id, "📭 Скринов не нашлось.")
            return True
        header = (
            "📸 Скрины " + st["nick"] + " за " + today_local().strftime("%d.%m.%Y")
            + " (" + ", ".join(LIST_TITLE[x] for x in st["lists"]) + "): " + str(len(all_items))
        )
        _send_shots(peer_id, header, all_items)
        if st.get("after") == "review":
            _review_shots_done(
                peer_id,
                "📸 Показал скрины " + st["nick"] + ": " + str(len(all_items)) + " шт. (см. выше).",
            )
    return True
# ==========================================================
# ОТЧЁТЫ: сторона СТРАНИЧНОГО БОТА (аккаунт пользователя, longpoll по user-токену)
# ==========================================================
def stop_page_bot():
    global PAGE_GEN, PAGE_JOB
    with page_lock:
        PAGE_GEN += 1
        PAGE_JOB = None
def start_page_bot():
    """(Пере)запускает поток страничного бота."""
    global PAGE_GEN, PAGE_THREAD, PAGE_JOB
    with page_lock:
        PAGE_GEN += 1
        PAGE_JOB = None
        gen = PAGE_GEN
        if not data.get("page", {}).get("token"):
            return
        t = threading.Thread(target=page_bot_loop, args=(gen,), daemon=True)
        PAGE_THREAD = t
        t.start()
def page_send(api, text):
    """Страничный бот (user-токен) пишет в диалог с сообществом."""
    api.messages.send(peer_id=-GROUP_ID, message=text, random_id=get_random_id())
def page_bot_loop(gen):
    print("Страничный бот: поток запущен.")
    while gen == PAGE_GEN:
        try:
            token = data.get("page", {}).get("token", "")
            if not token:
                return
            sess = vk_api.VkApi(token=token)
            api = sess.get_api()
            lp = VkLongPoll(sess, wait=25, mode=2)
            print("Страничный бот: слушаю сообщения от сообщества.")
            while gen == PAGE_GEN:
                for ev in lp.check():
                    if gen != PAGE_GEN:
                        break
                    if ev.type != VkEventType.MESSAGE_NEW:
                        continue
                    try:
                        page_on_message(api, ev)
                    except Exception as e:
                        print("Страничный бот: ошибка обработки:", repr(e))
        except Exception as e:
            print("Страничный бот: соединение прервано:", repr(e))
            time.sleep(8)
    print("Страничный бот: поток остановлен.")
def _page_message_text(api, ev):
    """Полный текст сообщения (longpoll может обрезать длинные)."""
    try:
        res = api.messages.getById(message_ids=ev.message_id)
        items = res.get("items") if isinstance(res, dict) else res
        if items:
            return items[0].get("text") or ""
    except Exception as e:
        print("Страничный бот: getById не удался:", repr(e))
    raw = getattr(ev, "text", "") or ""
    return html_lib.unescape(raw.replace("<br>", "\n"))
def page_send_att(api, text, attachment):
    api.messages.send(peer_id=-GROUP_ID, message=text, attachment=attachment, random_id=get_random_id())
def _comment_attachments(comment):
    result = []
    for a in comment.get("attachments") or []:
        t = a.get("type")
        if t == "photo":
            ph = a.get("photo") or {}
            s = "photo" + str(ph.get("owner_id")) + "_" + str(ph.get("id"))
            if ph.get("access_key"):
                s += "_" + str(ph["access_key"])
            result.append(s)
        elif t == "doc":
            dc = a.get("doc") or {}
            if (dc.get("ext") or "").lower() in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
                s = "doc" + str(dc.get("owner_id")) + "_" + str(dc.get("id"))
                if dc.get("access_key"):
                    s += "_" + str(dc["access_key"])
                result.append(s)
    return result
def page_run_shots(api, vk_id, lists):
    st = get_report_settings()
    group_id = st["group_id"]
    if not group_id:
        raise RuntimeError("не задано сообщество (/отгруппа)")
    today = today_local()
    start = datetime(today.year, today.month, today.day, tzinfo=MSK)
    ts_from = int(start.timestamp())
    ts_to = int((start + timedelta(days=1)).timestamp())

    found = []   # (date, attachment)
    for n in lists:
        topic_id = st["topic_otchety"] if n == 1 else st["topic_ot"]
        if not topic_id:
            continue
        comments = fetch_topic_comments(api, group_id, topic_id, ts_from, ts_to)
        for c in comments:
            if c.get("from_id") != vk_id:
                continue
            for att in _comment_attachments(c):
                found.append((c.get("date", 0), att))
    found.sort(key=lambda x: x[0])
    atts = list(dict.fromkeys(a for _, a in found))
    if not atts:
        page_send(api, "СКРИНЫ_НЕТ")
        return
    chunks = [atts[i:i + 10] for i in range(0, len(atts), 10)]
    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        page_send_att(api, "СКРИНЫ " + str(i) + "/" + str(total), ",".join(chunk))
        time.sleep(0.5)
def page_on_message(api, ev):
    global PAGE_JOB
    if getattr(ev, "from_me", False):
        return
    if getattr(ev, "peer_id", None) != -GROUP_ID:
        return
    text = _page_message_text(api, ev).strip()
    if not text:
        return
    low = text.lower()

    # Команда от основного бота: /скрины <vk_id> <списки>
    m = re.fullmatch(r"/скрины\s+(\d+)\s+([12](?:,[12])?)", low)
    if m:
        try:
            page_run_shots(api, int(m.group(1)), [int(x) for x in m.group(2).split(",")])
        except Exception as e:
            print("Страничный бот: ошибка сбора скринов:", repr(e))
            try:
                page_send(api, "ОШИБКА: " + str(e)[:300])
            except Exception as e2:
                print("Страничный бот: не удалось сообщить об ошибке:", repr(e2))
        return

    # Команда от основного бота: /от дд.мм.гггг  или  /отчеты дд.мм.гггг
    m = re.fullmatch(r"/(от|отчеты|отчёты)\s+(\d{2}\.\d{2}\.\d{4})", low)
    if m:
        target = parse_report_date(m.group(2))
        if target is None:
            return
        n = 2 if m.group(1) == "от" else 1
        PAGE_JOB = {
            "n": n, "date": m.group(2), "target": target,
            "parts": {}, "total": 0, "ts": time.time(),
        }
        page_send(api, "/нлист" if n == 2 else "/список")
        return

    # Список ников от основного бота
    m = re.match(r"НИКИ\s+(\d+)/(\d+)[ \t]*\n?(.*)", text, re.S)
    if m:
        job = PAGE_JOB
        if not job:
            return
        job["parts"][int(m.group(1))] = m.group(3)
        job["total"] = int(m.group(2))
        if len(job["parts"]) < job["total"]:
            return
        PAGE_JOB = None
        try:
            page_send(api, "ники получены, проверяю отчеты")
            page_run_check(api, job)
        except Exception as e:
            print("Страничный бот: ошибка проверки:", repr(e))
            try:
                page_send(api, "ОШИБКА: " + str(e)[:300])
            except Exception as e2:
                print("Страничный бот: не удалось сообщить об ошибке:", repr(e2))
def _count_images(comment):
    k = 0
    for a in comment.get("attachments") or []:
        t = a.get("type")
        if t == "photo":
            k += 1
        elif t == "doc":
            ext = ((a.get("doc") or {}).get("ext") or "").lower()
            if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
                k += 1
    return k
def fetch_topic_comments(api, group_id, topic_id, ts_from, ts_to):
    """Все комментарии обсуждения за период [ts_from, ts_to)."""
    result = []
    offset = 0
    while True:
        resp = api.board.getComments(
            group_id=group_id, topic_id=topic_id,
            sort="desc", count=100, offset=offset,
        )
        items = resp.get("items") or []
        if not items:
            break
        stop = False
        for c in items:
            d = c.get("date", 0)
            if d < ts_from:
                stop = True
                break
            if d < ts_to:
                result.append(c)
        offset += len(items)
        if stop or offset >= resp.get("count", 0):
            break
        time.sleep(0.35)
    return result
def page_run_check(api, job):
    n = job["n"]
    target = job["target"]
    st = get_report_settings()
    group_id = st["group_id"]
    topic_id = st["topic_otchety"] if n == 1 else st["topic_ot"]
    if not group_id or not topic_id:
        raise RuntimeError("не задано сообщество или обсуждение (/отместо)")

    # ники, которые прислал основной бот: Nick | vk_id | статус
    entries = []
    for part in sorted(job["parts"]):
        for line in job["parts"][part].splitlines():
            cols = [c.strip() for c in line.split("|")]
            if len(cols) < 3 or not cols[0]:
                continue
            try:
                vid = int(cols[1])
            except ValueError:
                vid = 0
            entries.append((cols[0], vid, cols[2]))

    # смотрим обсуждение за этот день (по МСК)
    shots = {}
    need_check = any(e[2].lower() == "работает" for e in entries)
    marker_ts = 0
    has_new_after_marker = False
    if need_check:
        start = datetime(target.year, target.month, target.day, tzinfo=MSK)
        ts_from = int(start.timestamp())
        ts_to = int((start + timedelta(days=1)).timestamp())
        comments = fetch_topic_comments(api, group_id, topic_id, ts_from, ts_to)
        # последняя метка «все выше отчеты проверены»: учитываем только то, что написано ПОСЛЕ неё
        for c in comments:
            if (c.get("text") or "").strip().lower() == MARKER_TEXT:
                marker_ts = max(marker_ts, int(c.get("date", 0) or 0))
        for c in comments:
            d = int(c.get("date", 0) or 0)
            if marker_ts and d <= marker_ts:
                continue
            has_new_after_marker = True
            author = c.get("from_id", 0)
            shots[author] = shots.get(author, 0) + _count_images(c)

    # пишем в обсуждении, что проверка закончена.
    # Если после прошлой метки никто ничего не написал — метку повторно не ставим.
    if not marker_ts or has_new_after_marker:
        try:
            api.board.createComment(
                group_id=group_id, topic_id=topic_id,
                message=MARKER_TEXT,
                guid=str(get_random_id()),
            )
        except Exception as e:
            print("Страничный бот: не удалось написать в обсуждение:", repr(e))

    # итог для основного бота
    lines = []
    for nick, vid, status in entries:
        if status.lower() == "работает":
            k = shots.get(vid, 0)
            if k > 0:
                lines.append(nick + " есть отчет " + str(k) + " " + plural_screens(k))
            elif marker_ts:
                # повторная проверка за день: после метки от этого игрока ничего нет —
                # основной бот подставит результат прошлой проверки
                lines.append(nick + " без изменений")
            else:
                lines.append(nick + " нет отчета")
        else:
            lines.append(nick + " " + status)   # «отгул» или «отпуск до дд.мм»
    chunks = chunk_lines(lines) or [""]
    total = len(chunks)
    for i, body in enumerate(chunks, 1):
        page_send(api, "РЕЗУЛЬТАТЫ " + str(i) + "/" + str(total) + "\n" + body)
        time.sleep(0.4)
# ==========================================================
# ОТЧЁТЫ: где проверять (сообщество + обсуждения)
# ==========================================================
def get_report_settings():
    """Актуальные настройки места проверки отчётов: сохранённые или из шапки файла."""
    with lock:
        saved = data.get("report_settings") or {}
    return _norm_report_settings(saved)
def topic_id_for_list(n):
    """Какой топик использовать: n=1 -> /отчеты, n=2 -> /от."""
    st = get_report_settings()
    return st["topic_otchety"] if n == 1 else st["topic_ot"]
def settings_warning(n):
    """Подсказка, если не задано сообщество или обсуждение."""
    st = get_report_settings()
    missing = []
    if not st["group_id"]:
        missing.append("сообщество (/отгруппа <id>)")
    if not topic_id_for_list(n):
        missing.append("обсуждение (/отобс %s)" % ("отчеты" if n == 1 else "от"))
    if not missing:
        return ""
    return "⚠️ Не задано: " + ", ".join(missing) + ".\n"
def cmd_report_settings(raw_cmd, args, user_id):
    """/отгруппа <id> — где лежат отчёты. /отобс от|отчеты <topic_id> — какое обсуждение.
    /отместо — показать текущие настройки. Только общие админы."""
    if user_id not in ALLOWED_IDS and not (raw_cmd == "/отместо" and user_id in NADMIN_IDS):
        return None
    st = get_report_settings()
    if raw_cmd == "/отместо":
        gid = st["group_id"]
        lines = ["📍 Где бот проверяет отчёты:"]
        lines.append("Сообщество: " + (str(gid) + " (vk.com/club" + str(gid) + ")" if gid else "не задано"))
        lines.append("Обсуждение для /от (список /нлист): " + (str(st["topic_ot"]) if st["topic_ot"] else "не задано"))
        lines.append("Обсуждение для /отчеты (список /список): " + (str(st["topic_otchety"]) if st["topic_otchety"] else "не задано"))
        lines.append(
            "\nЗадать:\n"
            "/отгруппа 123456\n"
            "/отобс от 1 — обсуждение для /от\n"
            "/отобс отчеты 2 — обсуждение для /отчеты"
        )
        return "\n".join(lines)
    if user_id not in ALLOWED_IDS:
        return None
    if raw_cmd == "/отгруппа":
        if len(args) != 1 or not re.fullmatch(r"-?\d+", args[0]):
            return "Формат: /отгруппа 123456"
        gid = abs(int(args[0]))
        if gid <= -1:
            return "ID сообщества должен быть больше -1."
        with lock:
            data["report_settings"] = {**st, "group_id": gid}
            save_data()
        return "✅ Сообщество для проверки отчётов: " + str(gid) + " (vk.com/club" + str(gid) + ")."
    if raw_cmd == "/отобс":
        if len(args) != 2 or not re.fullmatch(r"-?\d+", args[1]):
            return (
                "Формат: /отобс от 1\n"
                "/отобс от — обсуждение для команды /от (список /нлист)\n"
                "/отобс отчеты — обсуждение для /отчеты (список /список)"
            )
        which = args[0].lower()
        tid = abs(int(args[1]))
        if tid <= 0:
            return "ID обсуждения должен быть больше 0."
        if which in ("от", "ot", "2"):
            key = "topic_ot"
            title = "/от (список /нлист)"
        elif which in ("отчеты", "отчёты", "otchety", "1"):
            key = "topic_otchety"
            title = "/отчеты (список /список)"
        else:
            return "Укажите: /отобс от <id> или /отобс отчеты <id>"
        with lock:
            data["report_settings"] = {**st, key: tid}
            save_data()
        return "✅ Обсуждение для " + title + ": " + str(tid) + "."
    return None
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
        nick = player["nick"]
        num = idx + 1

        # сквозной номер покупки
        purchase_id = int(data.get("purchase_counter", 0) or 0) + 1
        data["purchase_counter"] = purchase_id

        # покупка в магазине НЕ влияет на баллы конкурса (они считают только заработанное)
        player["points"] -= item_price
        left = player["points"]

        # отгул и отпуск теперь выдаёт только админ — в магазине всегда kind="other"
        kind = "other"
        history = player.setdefault("purchases", [])
        history.append({
            "id": purchase_id,
            "item": item_name,
            "price": item_price,
            "num": num,
            "kind": kind,
            "ts": datetime.now(MSK).isoformat(timespec="seconds"),
        })
        player["purchases"] = history[-PURCHASE_HISTORY_LIMIT:]

        answer = "✅ Вы купили: " + item_name + " (покупка №" + str(purchase_id) + "). Осталось баллов: " + str(left) + "."

        save_data()
        text = (
            "Покупка №" + str(purchase_id) + ": [id" + str(user_id) + "|@" + nick + "] (" + nick + ") купил №" + str(num) + ": "
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
        pid = pending.get("pid")
        prefix = ("Покупка №" + str(pid) + ": ") if pid else ""
        notify_text = (
            prefix + "[id" + str(user_id) + "|@" + nick + "] (" + nick + ") купил отпуск до "
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
    name = " ".join(args[1:])
    if is_vacation_item(name) or name.lower().strip() == "отгул":
        return "Товары «отпуск» и «отгул» больше не продаются — их выдаёт только админ."
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
# HELP (текстовые версии — запасной вариант, если картинка не создалась)
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
    "/кто — показать привязанных игроков, которым не доходят сообщения бота\n"
    "/+магазин1 100 товар — добавить товар в магазин /список\n"
    "/+магазин2 100 товар — добавить товар в магазин /нлист\n"
    "/-магазин1 3 и /-магазин2 3 — удалить товар по номеру\n"
    "/разослать1 текст и /разослать2 текст — рассылка по списку\n"
    "/рассылка текст — рассылка всем, кто писал боту\n"
    "/ответить текст — (любой админ) ответом на обращение из /написать отправить ответ пользователю\n"
    "/выдать Nick_Name 5 — выдать баллы\n"
    "/забрать Nick_Name 5 — забрать баллы\n"
    "/откат НОМЕР — откатить покупку по её номеру (баллы не возвращаются)\n"
    "/откат НОМЕР вернуть — откатить покупку и вернуть баллы (например /откат 2 вернуть)\n"
    "(номер покупки указан в уведомлении «Покупка №N»; об откате все админы получают сообщение)\n"
    "/отвязать @username или Nick_Name — отвязать игрока\n"
    "/изменить Старый Новый — сменить ник\n"
    "(выдать/забрать/отвязать/изменить сами находят список по нику; "
    "если ник есть в обоих — добавьте 1 или 2: /выдать2 Nick 5)\n"
    "/вд @username — выдать полный доступ админа\n"
    "/снятьвд @username — забрать доступ админа\n"
    "/вдв @username — выдать доступ только к списку /нлист\n"
    "/снятьвдв @username — забрать доступ к /нлист\n\n"
    "Отгул / отпуск / смс:\n"
    "/отгул Ник_Name — выдать отгул (или снять, если уже стоит)\n"
    "/отпуск — показать кто в отпуске/отгуле\n"
    "/отпуск Ник_Name 15.12 — выдать отпуск до даты (или снять)\n"
    "/смс Ник_Name Текст — отправить игроку сообщение от администратора\n\n"
    "Конкурс (общие админы):\n"
    "/конкурс 10.11.2026 20.11.2026 — запустить конкурс (даты начала и конца)\n"
    "/конкурс — показать текущий конкурс\n"
    "/конкурс стоп — остановить и сбросить\n"
    "/итоги — кто сколько заработал баллов за конкурс (оба списка, от большего к меньшему)\n\n"
    "Отчёты (нужен подключённый страничный бот + настройки места проверки):\n"
    "/отместо — показать где проверяются отчёты\n"
    "/отгруппа 123456 — ID сообщества, где лежат отчёты\n"
    "/отобс от 1 — обсуждение для /от\n"
    "/отобс отчеты 2 — обсуждение для /отчеты\n"
    "/отчеты дд.мм.гггг — проверка отчётов для /список (общие админы)\n"
    "/скрины Nick_Name или @username — скрины игрока за сегодня из обсуждений\n"
    "/от дд.мм.гггг — проверка отчётов для /нлист (админы /вдв и общие)\n"
    "💡 После проверки бот предложит выдать баллы (10+ скринов = 10 баллов, 6-9 = 7).\n"
    "4-5 скринов: бот покажет их и спросит, выдавать ли 7 баллов.\n"
    "Меньше 4 скринов — отчёт не засчитывается (0 баллов).\n"
    "При повторной проверке баллы повторно не выдаются — доначисляется только разница.\n"
    "Кому баллы уже выдавались за эту дату — тому не придёт «вам не начислены баллы».\n\n"
    "Отгул и отпуск теперь выдаёт только админ — в магазине эти товары убраны. "
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
    "/откат НОМЕР — откатить покупку по её номеру (баллы не возвращаются)\n"
    "/откат НОМЕР вернуть — откатить покупку и вернуть баллы (например /откат 2 вернуть)\n"
    "/привязать @username Nick_Name — привязать игрока\n"
    "/кто — показать игроков /нлист, которым не доходят сообщения бота\n"
    "/отвязать @username или Nick_Name — отвязать игрока\n"
    "/изменить Старый Новый — сменить ник\n"
    "/разослать текст — рассылка привязанным игрокам\n"
    "/+магазин 100 товар — добавить товар в магазин\n"
    "/-магазин 3 — удалить товар из магазина по номеру\n\n"
    "Отгул / отпуск / смс:\n"
    "/отгул Ник_Name — выдать отгул (или снять)\n"
    "/отпуск — показать кто в отпуске/отгуле\n"
    "/отпуск Ник_Name 15.12 — выдать отпуск до даты (или снять)\n"
    "/смс Ник_Name Текст — отправить игроку сообщение от администратора\n\n"
    "Отчёты (нужен подключённый страничный бот):\n"
    "/от дд.мм.гггг — проверка отчётов для /нлист (админы /вдв и общие)\n"
    "/скрины Nick_Name или @username — скрины игрока за сегодня\n"
    "/отместо — показать где проверяются отчёты\n"
    "💡 После проверки бот спросит про выдачу баллов (10+ скринов = 10, 6-9 = 7) и рассылку.\n"
    "4-5 скринов: сначала общая выдача, затем бот покажет скрины такого игрока и спросит, выдавать ли ему 7 баллов.\n"
    "Меньше 4 скринов — отчёт не засчитывается (0 баллов). Доначисляется только разница.\n"
)
def cmd_help(user_id):
    if user_id in ALLOWED_IDS:
        return ADMIN_HELP
    if user_id in NADMIN_IDS:
        return NADMIN_HELP
    return USER_HELP
# ==========================================================
# КАРТИНКА-СПРАВКА ДЛЯ АДМИНОВ (рисуется самим ботом через Pillow)
# ==========================================================
# Разделы: (название, [(команда, короткое описание), ...])
ADMIN_SECTIONS = [
    ("Участники и баллы", [
        ("/список", "Участники и баллы (список 1)"),
        ("/нлист", "Участники второго списка"),
        ("/топ1  ·  /топ2", "Топ по баллам каждого списка"),
        ("/выдать Ник 5", "Выдать баллы"),
        ("/забрать Ник 5", "Забрать баллы"),
    ]),
    ("Игроки", [
        ("/привязать1|2 @юз Ник", "Привязать игрока к нику"),
        ("/отвязать @юз  или  Ник", "Отвязать игрока"),
        ("/изменить Старый Новый", "Сменить ник"),
        ("/кто", "Кому не доходят сообщения бота"),
    ]),
    ("Магазин", [
        ("/+магазин1|2 100 товар", "Добавить товар"),
        ("/-магазин1|2 3", "Удалить товар по номеру"),
    ]),
    ("Покупки", [
        ("/откат 5", "Откатить покупку №5"),
        ("/откат 5 вернуть", "Откатить и вернуть баллы"),
    ]),
    ("Отгул / отпуск / смс", [
        ("/отгул Ник", "Выдать или снять отгул"),
        ("/отпуск", "Кто сейчас в отпуске/отгуле"),
        ("/отпуск Ник 15.12", "Выдать или снять отпуск"),
        ("/смс Ник Текст", "Сообщение игроку от админа"),
    ]),
    ("Конкурс", [
        ("/конкурс 10.11 20.11", "Запустить конкурс (даты)"),
        ("/конкурс", "Показать текущий конкурс"),
        ("/конкурс стоп", "Остановить и сбросить"),
        ("/итоги", "Кто сколько заработал баллов"),
    ]),
    ("Рассылки", [
        ("/разослать1|2 текст", "Всем игрокам списка"),
        ("/рассылка текст", "Всем, кто писал боту"),
        ("/ответить текст", "Ответ на обращение (reply)"),
    ]),
    ("Отчёты", [
        ("/отчеты дд.мм.гггг", "Проверка отчётов /список"),
        ("/от дд.мм.гггг", "Проверка отчётов /нлист"),
        ("/скрины Ник или @юз", "Скрины игрока за сегодня"),
        ("/отгруппа 123456", "Сообщество с отчётами"),
        ("/отобс от 1  ·  /отобс отчеты 2", "Обсуждения для проверки"),
        ("/отместо", "Где сейчас проверяются отчёты"),
    ]),
    ("Доступ", [
        ("/вд @юз", "Полный доступ админа"),
        ("/снятьвд @юз", "Забрать полный доступ"),
        ("/вдв @юз", "Доступ только к /нлист"),
        ("/снятьвдв @юз", "Забрать доступ к /нлист"),
    ]),
    ("Для всех", [
        ("/меню", "Показать кнопки"),
        ("/help", "Эта справка"),
    ]),
]
NADMIN_SECTIONS = [
    ("Участники и баллы", [
        ("/нлист", "Список участников"),
        ("/выдать Ник 5", "Выдать баллы"),
        ("/забрать Ник 5", "Забрать баллы"),
    ]),
    ("Игроки", [
        ("/привязать @юз Ник", "Привязать игрока к нику"),
        ("/отвязать @юз  или  Ник", "Отвязать игрока"),
        ("/изменить Старый Новый", "Сменить ник"),
        ("/кто", "Кому не доходят сообщения бота"),
    ]),
    ("Магазин", [
        ("/+магазин 100 товар", "Добавить товар"),
        ("/-магазин 3", "Удалить товар по номеру"),
    ]),
    ("Покупки", [
        ("/откат 5", "Откатить покупку №5"),
        ("/откат 5 вернуть", "Откатить и вернуть баллы"),
    ]),
    ("Отгул / отпуск / смс", [
        ("/отгул Ник", "Выдать или снять отгул"),
        ("/отпуск", "Кто сейчас в отпуске/отгуле"),
        ("/отпуск Ник 15.12", "Выдать или снять отпуск"),
        ("/смс Ник Текст", "Сообщение игроку от админа"),
    ]),
    ("Отчёты", [
        ("/от дд.мм.гггг", "Проверка отчётов /нлист"),
        ("/скрины Ник или @юз", "Скрины игрока за сегодня"),
        ("/отместо", "Где сейчас проверяются отчёты"),
    ]),
    ("Рассылка", [
        ("/разослать текст", "Всем игрокам /нлист"),
    ]),
    ("Для всех", [
        ("/меню", "Показать кнопки"),
        ("/help", "Эта справка"),
    ]),
]
ADMIN_CAPTION = (
    "📖 Пояснения к командам\n\n"
    "• Томас лох"
)
NADMIN_CAPTION = (
    "📖 Пояснения к командам\n\n"
    "Томас лох"
)
# ---------- шрифты ----------
_FONT_PATHS = {
    False: [
        os.path.join(FONT_DIR, "font.ttf"),
        os.path.join(FONT_DIR, "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
    True: [
        os.path.join(FONT_DIR, "font_bold.ttf"),
        os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ],
}
_FONT_URLS = {
    False: [
        "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans.ttf",
        "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/DejaVuSans.ttf",
    ],
    True: [
        "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans-Bold.ttf",
        "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/DejaVuSans-Bold.ttf",
    ],
}
_font_cache = {}
def _find_font(bold):
    """Путь к TTF-шрифту с кириллицей. Ищет в системе, в папке fonts/, иначе скачивает DejaVu."""
    if bold in _font_cache:
        return _font_cache[bold]
    for path in _FONT_PATHS[bold]:
        if os.path.exists(path):
            _font_cache[bold] = path
            return path
    target = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    for url in _FONT_URLS[bold]:
        try:
            os.makedirs(FONT_DIR, exist_ok=True)
            req = urllib.request.Request(url, headers={"User-Agent": "vk-points-bot"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read()
            if len(content) < 10000:
                continue
            with open(target, "wb") as f:
                f.write(content)
            print("Шрифт скачан:", target)
            _font_cache[bold] = target
            return target
        except Exception as e:
            print("Не удалось скачать шрифт:", repr(e))
    return None
def _fit_font(draw, text, path, size, max_width, min_size=16):
    """Подбирает размер шрифта так, чтобы текст влез в max_width."""
    while size > min_size:
        font = ImageFont.truetype(path, size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 1
    return ImageFont.truetype(path, min_size)
# ---------- рисование ----------
_PALETTE = [
    (255, 170, 70),
    (90, 200, 250),
    (120, 220, 140),
    (255, 110, 140),
    (190, 145, 255),
    (255, 215, 90),
    (100, 230, 210),
]
def render_help_image(title, subtitle, sections):
    """Рисует красивую картинку со списком команд. Возвращает BytesIO (PNG)."""
    if not PIL_OK:
        raise RuntimeError("Pillow не установлен")
    reg = _find_font(False)
    bold = _find_font(True) or reg
    if not reg:
        raise RuntimeError("не найден шрифт с кириллицей")

    W = 1600
    M = 40
    GAP = 40
    CW = (W - 2 * M - GAP) // 2
    HEAD_H = 84
    ROW_H = 92
    PAD = 22
    TOP = 200

    # раскладка карточек по двум колонкам (в более короткую)
    cols = [TOP, TOP]
    placed = []
    for idx, (name, rows) in enumerate(sections):
        h = HEAD_H + len(rows) * ROW_H + PAD
        col = 0 if cols[0] <= cols[1] else 1
        x = M + col * (CW + GAP)
        y = cols[col]
        placed.append((idx, name, rows, x, y, h))
        cols[col] += h + GAP
    H = max(cols) + 70

    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    # фон: плавный градиент
    for y in range(H):
        t = y / max(1, H - 1)
        d.line([(0, y), (W, y)], fill=(int(16 + 24 * t), int(18 + 8 * t), int(40 + 34 * t)))

    # заголовок
    f_title = ImageFont.truetype(bold, 70)
    f_sub = ImageFont.truetype(reg, 32)
    d.text((M + 6, 38), title, font=f_title, fill=(255, 255, 255))
    d.text((M + 8, 122), subtitle, font=f_sub, fill=(170, 180, 215))
    # цветная полоска под заголовком
    seg = (W - 2 * M) // len(_PALETTE)
    for i, color in enumerate(_PALETTE):
        d.rounded_rectangle((M + i * seg, 168, M + (i + 1) * seg - 6, 176), radius=4, fill=color)

    f_head = ImageFont.truetype(bold, 38)
    f_num = ImageFont.truetype(bold, 30)
    for idx, name, rows, x, y, h in placed:
        color = _PALETTE[idx % len(_PALETTE)]
        # карточка
        d.rounded_rectangle((x, y, x + CW, y + h), radius=28, fill=(29, 34, 66), outline=(56, 64, 108), width=2)
        # номер раздела
        d.ellipse((x + 24, y + 20, x + 68, y + 64), fill=color)
        num = str(idx + 1)
        nw = d.textlength(num, font=f_num)
        d.text((x + 46 - nw / 2, y + 28), num, font=f_num, fill=(20, 22, 40))
        d.text((x + 86, y + 20), name, font=f_head, fill=color)
        d.line((x + 24, y + HEAD_H - 6, x + CW - 24, y + HEAD_H - 6), fill=color, width=2)
        for i, (cmd, desc) in enumerate(rows):
            ry = y + HEAD_H + i * ROW_H
            max_w = CW - 56
            f_cmd = _fit_font(d, cmd, bold, 34, max_w)
            f_desc = _fit_font(d, desc, reg, 27, max_w)
            d.text((x + 28, ry + 4), cmd, font=f_cmd, fill=(255, 255, 255))
            d.text((x + 28, ry + 48), desc, font=f_desc, fill=(155, 166, 204))
            if i < len(rows) - 1:
                sy = ry + ROW_H - 6
                d.line((x + 28, sy, x + CW - 28, sy), fill=(46, 54, 94), width=1)

    f_foot = ImageFont.truetype(reg, 26)
    foot = "Подробные пояснения — в сообщении под картинкой"
    fw = d.textlength(foot, font=f_foot)
    d.text(((W - fw) / 2, H - 52), foot, font=f_foot, fill=(130, 142, 185))

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    buf.name = "help.png"
    return buf
# ---------- загрузка в VK (один раз, дальше берётся из кэша) ----------
HELP_PHOTO = {}
help_lock = threading.Lock()
def get_help_photo(kind):
    """Возвращает attachment-строку картинки ('admin' или 'nadmin') или None, если не вышло."""
    with help_lock:
        cached = HELP_PHOTO.get(kind)
        if cached:
            return cached
        if not PIL_OK:
            print("Pillow не установлен — картинка-справка отключена (pip install Pillow).")
            return None
        try:
            if kind == "admin":
                buf = render_help_image(
                    "Команды администратора",
                    "Полный доступ  ·  два списка: /список (1) и /нлист (2)",
                    ADMIN_SECTIONS,
                )
            else:
                buf = render_help_image(
                    "Команды администратора /нлист",
                    "Доступ по /вдв  ·  работаете только со списком /нлист",
                    NADMIN_SECTIONS,
                )
            upload = vk_api.VkUpload(vk_session)
            photos = upload.photo_messages(photos=buf)
            ph = photos[0]
            attachment = "photo" + str(ph["owner_id"]) + "_" + str(ph["id"])
            if ph.get("access_key"):
                attachment += "_" + str(ph["access_key"])
            HELP_PHOTO[kind] = attachment
            print("Картинка-справка '" + kind + "' загружена:", attachment)
            return attachment
        except Exception as e:
            print("Не удалось создать/загрузить картинку-справку '" + kind + "':", repr(e))
            return None
def send_admin_help(peer_id, user_id, keyboard=None):
    """Админам: картинка со всеми командами + пояснения текстом под ней."""
    if user_id in ALLOWED_IDS:
        kind, caption, fallback = "admin", ADMIN_CAPTION, ADMIN_HELP
    else:
        kind, caption, fallback = "nadmin", NADMIN_CAPTION, NADMIN_HELP
    for _ in range(2):
        attachment = get_help_photo(kind)
        if not attachment:
            break
        try:
            params = {
                "peer_id": peer_id,
                "message": caption,
                "attachment": attachment,
                "random_id": get_random_id(),
            }
            if keyboard:
                params["keyboard"] = keyboard
            vk.messages.send(**params)
            return
        except Exception as e:
            print("Ошибка отправки картинки-справки:", repr(e))
            with help_lock:
                HELP_PHOTO.pop(kind, None)  # перезагрузим картинку заново
    params = {"peer_id": peer_id, "message": fallback, "random_id": get_random_id()}
    if keyboard:
        params["keyboard"] = keyboard
    vk.messages.send(**params)
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
def handle(text, user_id, peer_id=0):
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

    # Конкурс — только общие админы: /конкурс дата дата, /итоги
    if raw_cmd in ("/конкурс", "/конкрус"):
        return cmd_contest(args) if is_common else None
    if raw_cmd == "/итоги":
        return cmd_contest_results() if is_common else None

    # Кому не доходят сообщения бота: /кто (общие админы — оба списка, /вдв — только /нлист)
    if raw_cmd == "/кто":
        if not is_common and not is_nadmin:
            return None
        return cmd_who(peer_id, is_common)

    # Отчёты — общие админы: /отчеты дд.мм.гггг, /вдв админы: /от дд.мм.гггг
    if raw_cmd in ("/отчеты", "/отчёты"):
        if not is_common:
            return None
        if len(args) != 1:
            return "Формат: /отчеты дд.мм.гггг"
        warn = settings_warning(1)
        if warn:
            return warn
        return cmd_reports_start(args[0], 1, peer_id)
    if raw_cmd == "/от":
        if not is_nadmin and not is_common:
            return None
        if len(args) != 1:
            return "Формат: /от дд.мм.гггг"
        warn = settings_warning(2)
        if warn:
            return warn
        return cmd_reports_start(args[0], 2, peer_id)

    # Скрины игрока за сегодня: /скрины Nick или @username
    if raw_cmd in ("/скрины", "/скины"):
        if not is_common and not is_nadmin:
            return None
        return cmd_shots(args, user_id, peer_id, is_common)

    # Настройки «где проверять отчёты» — только общие админы
    if raw_cmd == "/отместо":
        return cmd_report_settings(raw_cmd, args, user_id)
    if raw_cmd == "/отгруппа":
        return cmd_report_settings(raw_cmd, args, user_id)
    if raw_cmd == "/отобс":
        return cmd_report_settings(raw_cmd, args, user_id)

    # /отгул — выдать/снять отгул (общие админы — оба списка, /вдв — /нлист)
    if raw_cmd == "/отгул":
        if not is_common and not is_nadmin:
            return None
        return cmd_otgul(args, is_common)
    # /отпуск — выдать/снять отпуск (общие админы — оба списка, /вдв — /нлист)
    if raw_cmd == "/отпуск":
        if not is_common and not is_nadmin:
            return None
        return cmd_otpusk(args, is_common)
    # /смс — отправить сообщение игроку (общие админы — оба списка, /вдв — /нлист)
    if raw_cmd == "/смс":
        if not is_common and not is_nadmin:
            return None
        return cmd_sms(args, is_common, user_id)

    command, suffix = split_command(raw_cmd)
    if command not in LIST_COMMANDS or command in ("/баллы", "/магазин", "/купить", "/топ"):
        return None

    # Откат по номеру покупки (список определяется по самой покупке)
    if command == "/откат":
        if not is_common and not is_nadmin:
            return None
        return cmd_rollback(args, user_id, is_common)

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
def parse_payload(msg):
    raw = msg.get("payload")
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None
def is_any_admin(user_id):
    return user_id in ALLOWED_IDS or user_id in NADMIN_IDS
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

    # === Кнопки «Да/Нет» по отчётам (работают и в чатах, и в ЛС) ===
    if payload and payload.get("c") == "rep_answer":
        try:
            result = report_answer_response(from_id, peer_id, payload)
        except Exception as e:
            print("Ошибка кнопки по отчётам:", repr(e))
            result = "Произошла ошибка. Попробуйте позже.", None
        if result is not None:
            answer, kb = result
            new_id = reply(peer_id, answer, kb)
            # сохраняем ID нового вопроса, чтобы потом убрать кнопки
            if kb and new_id:
                with lock:
                    st = REPORT_AWAIT.get(peer_id)
                    if st is not None:
                        st["msg_id"] = new_id
        return

    # === Нажатия на кнопки (только ЛС) ===
    if is_dm and payload:
        if payload.get("command") == "start":
            reply(peer_id, WELCOME_TEXT, menu_keyboard())
            return
        if payload.get("c"):
            # админам кнопка «Помощь» тоже показывает картинку
            if payload.get("c") == "help" and is_any_admin(from_id):
                send_admin_help(peer_id, from_id)
                return
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

    # === Секретная команда +токен (только в ЛС; в /help не светится) ===
    if is_dm and text.startswith("+токен"):
        rest = text[len("+токен"):].strip()
        answer = cmd_report_connect(from_id, rest)
        reply(peer_id, answer)
        return

    # === Секретная команда -токен: отключить страничного бота (только общие админы) ===
    if is_dm and text.startswith("-токен") and from_id in ALLOWED_IDS:
        reply(peer_id, cmd_report_disconnect(from_id))
        return

    # ======================================================
    # ОТЧЁТЫ и СКРИНЫ: диалог со страничным ботом (его сообщения в ЛС сообщества).
    # Если активной проверки нет — сообщение обрабатывается как обычное.
    # ======================================================
    if is_dm and is_page_connected() and from_id == get_page_user_id():
        try:
            if handle_page_shots(msg):
                return
            if handle_page_dialog(from_id, text):
                return
        except Exception as e:
            print("Ошибка диалога со страничным ботом:", repr(e))
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

    # /help и /хелп: админам (полный доступ и /вдв) — картинка + пояснения
    # Заодно снимаем все кнопки в этом чате (вопросы по отчётам и меню-клавиатуру).
    if raw_cmd in ("/help", "/хелп") and is_any_admin(from_id):
        close_chat_questions(peer_id)
        try:
            send_admin_help(peer_id, from_id, hide_keyboard())
        except Exception as e:
            print("Ошибка справки:", repr(e))
            reply(peer_id, cmd_help(from_id), hide_keyboard())
        return

    is_public = True
    public_answer = None
    public_keyboard = None
    try:
        if command == "/баллы":
            n = public_ctx(from_id, suffix)
            public_answer = "Формат: /баллы" if args else cmd_points(from_id, n)
        elif raw_cmd in ("/help", "/хелп"):
            close_chat_questions(peer_id)
            public_answer = cmd_help(from_id)
            public_keyboard = hide_keyboard()
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
        elif raw_cmd in ("/ответить", "/ответ"):
            public_answer = cmd_reply_to_user(msg, from_id, text)
        else:
            is_public = False
    except Exception as e:
        print("Ошибка публичной команды:", repr(e))
        public_answer = "Произошла ошибка. Попробуйте позже."
    if is_public:
        if public_answer:
            reply(peer_id, public_answer, public_keyboard)
        return
    # ======================================================
    # ВСЕ ОСТАЛЬНЫЕ КОМАНДЫ — только админы (общие или /вдв)
    # ======================================================
    if from_id not in ALLOWED_IDS and from_id not in NADMIN_IDS:
        return
    try:
        answer = handle(text, from_id, peer_id)
    except Exception as e:
        print("Ошибка команды:", repr(e))
        answer = "Произошла ошибка при выполнении команды."
    if answer:
        reply_long(peer_id, answer)
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
        + " | покупок всего: " + str(data.get("purchase_counter", 0))
    )
    print("Владелец обращений (/написать, если игрок никому не писал):", OWNER_ID)
    print("Сегодня (МСК):", today_local().isoformat())
    c_start, c_end = contest_period()
    if c_start is not None:
        print("Конкурс:", c_start.isoformat(), "—", c_end.isoformat(), "| игроков с баллами:", len(data["contest"].get("earned", {})))
    page = data.get("page", {})
    if page.get("token"):
        print("Страничный бот подключён: user=" + str(page.get("user_id")))
        start_page_bot()
    else:
        print("Страничный бот не подключён. Для подключения: напиши боту в ЛС +токен vk1.a.xxxxx")
    rst = get_report_settings()
    print(
        "Проверка отчётов — сообщество:", rst["group_id"] or "не задано (/отгруппа)",
        "| обсуждение /от:", rst["topic_ot"] or "не задано (/отобс от)",
        "| обсуждение /отчеты:", rst["topic_otchety"] or "не задано (/отобс отчеты)",
    )
    # заранее рисуем и загружаем картинки-справки, чтобы /help отвечал сразу
    executor.submit(get_help_photo, "admin")
    executor.submit(get_help_photo, "nadmin")
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
