import os
import re
import time
import requests
import vk_api

from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

# ==========================================================
# НАСТРОЙКИ
# ==========================================================

VK_TOKEN = os.environ.get(
    "VK_TOKEN",
    "vk1.a._74jNUH2XsupAUEs4x9E77nW5dOFjH4W8_fnoLzl9Aq5weI3PG6rOCiFIOJTq0HxxuTWwrsN13LsSDyiQYLB0r9oXg5ychaqmrySRp76k4_RGnQjo94fsIWw3lEOBhhDSo8p9ugMehGZMl4mGSEFgmjacafPAHLlQxSqz1PO6msQWXb7MWQIQjMbinOTB6duGmJCnykldQBot2b5_0xOJA"
).strip().strip('"').strip("'")

GROUP_ID = 241841230

SCRIPT_URL = "https://script.google.com/macros/s/AKfycbxF9rlgFvZUiy4_Ywn3bZEOAH1bvpurT0TMdaIi3-BHhGxV85HM7z_u5kqcMa_EPx9A/exec"

# Должен совпадать с SECRET в Google Apps Script
SCRIPT_SECRET = "724422"

# VK ID пользователей, которым доступны админские команды
ALLOWED_IDS = {
    875762552
}

DEBUG = False

# ==========================================================
# ПОДКЛЮЧЕНИЕ К VK
# ==========================================================

vk_session = vk_api.VkApi(
    token=VK_TOKEN
)

vk = vk_session.get_api()

# ==========================================================
# HTTP-СЕССИЯ
# ==========================================================

http = requests.Session()

http.headers.update({
    "Content-Type": "application/json"
})

# ==========================================================
# ОТПРАВКА СООБЩЕНИЯ
# ==========================================================

def reply(peer_id, text):

    try:

        vk.messages.send(
            peer_id=peer_id,
            message=str(text),
            random_id=0
        )

    except Exception as e:

        print(
            "Ошибка отправки сообщения:",
            repr(e)
        )

# ==========================================================
# ЗАПРОС В GOOGLE APPS SCRIPT
# ==========================================================

def call_sheet(payload):

    payload["secret"] = SCRIPT_SECRET

    try:

        response = http.post(
            SCRIPT_URL,
            json=payload,
            timeout=15,
            allow_redirects=True
        )

        if DEBUG:

            print(
                "Apps Script HTTP:",
                response.status_code
            )

            print(
                "Apps Script response:",
                response.text[:2000]
            )

        if response.status_code != 200:

            print(
                "Apps Script HTTP ошибка:",
                response.status_code,
                response.text[:1000]
            )

            return (
                "Ошибка таблицы. "
                f"HTTP {response.status_code}"
            )

        raw = response.text.strip()

        raw = raw.lstrip("\ufeff")

        if not raw:

            return (
                "Таблица вернула "
                "пустой ответ."
            )

        try:

            data = response.json()

        except ValueError:

            print(
                "Apps Script вернул НЕ JSON:"
            )

            print(
                raw[:2000]
            )

            return (
                "Ошибка ответа таблицы. "
                "Проверьте SCRIPT_URL "
                "и развёртывание Apps Script."
            )

        if data.get("ok") is False:

            return str(data.get(
                    "message",
                    "Неизвестная ошибка таблицы."
                )
            )

        message = data.get("message")

        if message is not None:

            return str(message)

        return (
            "Таблица вернула "
            "пустой ответ."
        )

    except requests.Timeout:

        return (
            "Таблица отвечает слишком долго. "
            "Попробуйте ещё раз."
        )

    except requests.RequestException as e:

        print(
            "Ошибка соединения с таблицей:",
            repr(e)
        )

        return (
            "Не удалось связаться "
            "с таблицей."
        )

# ==========================================================
# ВЫДАТЬ / ЗАБРАТЬ
# ==========================================================

def cmd_give(args, sign):

    if (
        len(args) != 2
        or not re.fullmatch(
            r"\d+",
            args[1]
        )
    ):

        command = (
            "/выдать"
            if sign > 0
            else "/забрать"
        )

        return (
            f"Формат: "
            f"{command} Nick_Name 5"
        )

    amount = int(args[1])

    if amount <= 0:

        return (
            "Количество баллов "
            "должно быть больше 0."
        )

    return call_sheet({

        "action": "points",

        "nick": args[0],

        "delta":
            sign * amount

    })

# ==========================================================
# ОПРЕДЕЛЕНИЕ VK ID
# ==========================================================

def resolve_user(raw):

    raw = raw.strip()

    # Формат:
    # Имя

    match = re.fullmatch(
        r"\[id(\d+)\|.*\]",
        raw
    )

    if match:

        return int(
            match.group(1)
        )

    username = (
        raw
        .lstrip("@")
        .strip()
    )

    if not username:

        return None

    try:

        users = vk.users.get(
            user_ids=username
        )

        if not users:

            return None

        return users[0]["id"]

    except vk_api.ApiError as e:

        print(
            "Ошибка поиска VK:",
            repr(e)
        )

        return None

# ==========================================================
# ПРИВЯЗАТЬ
# ==========================================================

def cmd_bind(args):

    if len(args) != 4:

        return (
            "Формат:\n"
            "/привязать "
            "@username Nick_Name по 25/09"
        )

    username = args[0]

    nick = args[1]

    position = args[2].lower()

    date = args[3]

    if position not in (
        "по",
        "зо"
    ):

        return (
            "Должность должна быть "
            "«по» или «зо»."
        )

    if not re.fullmatch(
        r"\d{2}/\d{2}",
        date
    ):

        return (
            "Дата должна быть "
            "в формате дд/мм."
        )

    user_id = resolve_user(
        username
    )

    if user_id is None:

        return (
            f"Пользователь "
            f"{username} не найден в VK."
        )

    return call_sheet({

        "action": "bind",

        "nick": nick,

        "position": position,

        "date": date,

        "link":
            f"https://vk.com/id{user_id}"

    })

# ==========================================================
# УБРАТЬ
# ==========================================================

def cmd_remove(args):

    if len(args) != 1:

        return (
            "Формат: /убрать @username"
        )

    user_id = resolve_user(
        args[0]
    )

    if user_id is None:

        return (
            f"Пользователь "
            f"{args[0]} не найден в VK."
        )

    return call_sheet({

        "action": "remove",

        "link":
            f"https://vk.com/id{user_id}"

    })

# ==========================================================
# СПИСОК
# ==========================================================

def cmd_list(args):

    if args:

        return (
            "Формат: /список"
        )

    return call_sheet({

        "action": "list"

    })

# ==========================================================
# БАЛЛЫ ТЕКУЩЕГО ПОЛЬЗОВАТЕЛЯ
# ==========================================================

def cmd_points(user_id):

    return call_sheet({

        "action": "my_points",

        "link":
            f"https://vk.com/id{user_id}"

    })

# ==========================================================
# ОБРАБОТКА КОМАНД
# ==========================================================

def handle(text, user_id):

    parts = (
        text
        .strip()
        .split()
    )

    if not parts:

        return None

    command = (
        parts[0]
        .lower()
    )

    args = parts[1:]

    # ------------------------------------------------------
    # БАЛЛЫ
    # Доступно ВСЕМ пользователям
    # ------------------------------------------------------

    if command == "/баллы":

        if args:

            return (
                "Формат: /баллы"
            )

        return cmd_points(
            user_id
        )

    # ------------------------------------------------------
    # ВЫДАТЬ
    # Только администратор
    # ------------------------------------------------------

    if command == "/выдать":

        return cmd_give(
            args,
            +1
        )

    # ------------------------------------------------------
    # ЗАБРАТЬ
    # Только администратор
    # ------------------------------------------------------

    if command == "/забрать":

        return cmd_give(
            args,
            -1
        )

    # ------------------------------------------------------
    # ПРИВЯЗАТЬ
    # Только администратор
    # ------------------------------------------------------

    if command == "/привязать":

        return cmd_bind(
            args
        )

    # ------------------------------------------------------
    # УБРАТЬ
    # Только администратор
    # ------------------------------------------------------

    if command == "/убрать":

        return cmd_remove(
            args
        )

    # ------------------------------------------------------
    # СПИСОК
    # Только администратор
    # ------------------------------------------------------

    if command == "/список":

        return cmd_list(
            args
        )

    return None

# ==========================================================
# ОБРАБОТКА СОБЫТИЯ VK
# ==========================================================

def process_event(event):

    if DEBUG:

        print(
            "Событие:",
            event.type
        )

    if (
        event.type
        != VkBotEventType.MESSAGE_NEW
    ):

        return

    msg = event.obj.message

    text = msg.get(
        "text",
        ""
    ).strip()

    peer_id = msg["peer_id"]

    from_id = msg["from_id"]

    if DEBUG:

        print(
            f"Сообщение от {from_id}: "
            f"{text!r}"
        )

    # Игнорируем сообщения
    # без команд

    if not text.startswith("/"):

        return

    # ======================================================
    # /БАЛЛЫ
    #
    # ВАЖНО:
    # Эта команда проверяется ДО ALLOWED_IDS.
    #
    # Поэтому /баллы работает для ВСЕХ.
    # ======================================================

    if text.lower() == "/баллы":

        try:

            answer = cmd_points(
                from_id
            )

        except Exception as e:

            print(
                "Ошибка команды /баллы:",
                repr(e)
            )

            answer = (
                "Произошла ошибка "
                "при получении баллов."
            )

        if answer:

            reply(
                peer_id,
                answer
            )

        return

    # ======================================================
    # ВСЕ ОСТАЛЬНЫЕ КОМАНДЫ
    #
    # Только пользователи из ALLOWED_IDS# ======================================================

    if from_id not in ALLOWED_IDS:

        reply(
            peer_id,
            f"Нет доступа. "
            f"Ваш id: {from_id}"
        )

        return

    # ======================================================
    # ВЫПОЛНЕНИЕ АДМИНСКОЙ КОМАНДЫ
    # ======================================================

    try:

        answer = handle(
            text,
            from_id
        )

    except Exception as e:

        print(
            "Ошибка команды:",
            repr(e)
        )

        answer = (
            "Произошла ошибка "
            "при выполнении команды."
        )

    if answer:

        reply(
            peer_id,
            answer
        )

# ==========================================================
# ЗАПУСК БОТА
# ==========================================================

def main():

    while True:

        try:

            longpoll = VkBotLongPoll(
                vk_session,
                GROUP_ID
            )

            print(
                "Бот запущен. "
                "Жду сообщения..."
            )

            for event in longpoll.listen():

                try:

                    process_event(
                        event
                    )

                except Exception as e:

                    print(
                        "Ошибка обработки:",
                        repr(e)
                    )

        except vk_api.exceptions.ApiError as e:

            print(
                "VK API:",
                repr(e)
            )

            time.sleep(3)

        except Exception as e:

            print(
                "Соединение прервано:",
                repr(e)
            )

            time.sleep(2)

# ==========================================================
# START
# ==========================================================

if __name__ == "__main__":

    main()
