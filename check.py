import vk_api

VK_TOKEN = "vk1.a._74jNUH2XsupAUEs4x9E77nW5dOFjH4W8_fnoLzl9Aq5weI3PG6rOCiFIOJTq0HxxuTWwrsN13LsSDyiQYLB0r9oXg5ychaqmrySRp76k4_RGnQjo94fsIWw3lEOBhhDSo8p9ugMehGZMl4mGSEFgmjacafPAHLlQxSqz1PO6msQWXb7MWQIQjMbinOTB6duGmJCnykldQBot2b5_0xOJA"
GROUP_ID = 241841230  # тот же id, что в bot.py

vk = vk_api.VkApi(token=VK_TOKEN).get_api()

# 1. Какому сообществу принадлежит токен
info = vk.groups.getById()
if isinstance(info, dict):
    info = info.get("groups", [])
print("Токен от сообщества:", info[0]["name"], "| id:", info[0]["id"])

# 2. Текущие настройки Long Poll
try:
    s = vk.groups.getLongPollSettings(group_id=GROUP_ID)
    print("Long Poll включён:", s.get("is_enabled"))
    print("Версия API:", s.get("api_version"))
    print("Входящие сообщения (message_new):", s.get("events", {}).get("message_new"))
except Exception as e:
    print("Не удалось прочитать настройки:", e)

# 3. Попытка включить всё нужное
try:
    vk.groups.setLongPollSettings(
        group_id=GROUP_ID,
        enabled=1,
        api_version="5.199",
        message_new=1,
    )
    print("Настройки Long Poll обновлены: включён, message_new = 1")
except Exception as e:
    print("Не удалось включить автоматически:", e)