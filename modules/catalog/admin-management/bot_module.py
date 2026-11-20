"""Interactive Telegram admin management for the AmneziaWG bot."""
import re


MODULE_KEY = "adm"
VERSION = "1.0.0"
TITLE = "Администраторы"
BUTTON = "👮 Администраторы"
DESCRIPTION = "Просмотр списка и добавление администратора по Telegram ID."

PENDING = {}


def init(_api):
    return None


def main_button(api):
    return [api["btn"](BUTTON, "mod:adm:home")]


def _home(api, note=""):
    admins = api["admin_ids"]()
    lines = ["<b>👮 Администраторы бота</b>", ""]
    if note:
        lines.extend([api["e"](note), ""])
    if admins:
        lines.append("Текущие Telegram ID:")
        lines.extend("• <code>%s</code>" % value for value in admins)
    else:
        lines.append("Список администраторов пуст.")
    keyboard = [
        [api["btn"]("➕ Добавить администратора", "mod:adm:add")],
        [api["btn"]("🔄 Обновить список", "mod:adm:home")],
        [api["btn"]("⬅️ Меню", "menu")],
    ]
    return "\n".join(lines), keyboard


def _is_private(message):
    chat = message.get("chat") or {}
    return chat.get("type") == "private"


def handle_callback(api, callback, action):
    message = callback.get("message") or {}
    chat = (message.get("chat") or {}).get("id")
    mid = message.get("message_id")
    user_id = (callback.get("from") or {}).get("id")
    if chat is None or mid is None or not api["is_admin"](user_id):
        return

    def show(view):
        api["edit"](chat, mid, *view)

    if action == "home":
        PENDING.pop(chat, None)
        show(_home(api))
    elif action == "add":
        if not _is_private(message):
            show(("Откройте личный диалог с ботом, чтобы добавлять администраторов.",
                  [[api["btn"]("⬅️ К списку", "mod:adm:home")]]))
            return
        PENDING[chat] = user_id
        show(("Отправьте числовой Telegram ID нового администратора.",
              [[api["btn"]("❌ Отмена", "mod:adm:cancel")]]))
    elif action == "cancel":
        if PENDING.get(chat) == user_id:
            PENDING.pop(chat, None)
        show(_home(api, "Добавление отменено."))


def handle_message(api, message):
    chat = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    if chat is None or PENDING.get(chat) != user_id or not api["is_admin"](user_id):
        return False
    if not _is_private(message):
        PENDING.pop(chat, None)
        return False

    text = (message.get("text") or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,15}", text):
        api["send"](chat, "Нужен числовой Telegram ID. Отправьте ID или отмените добавление кнопкой.",
                    [[api["btn"]("❌ Отмена", "mod:adm:cancel")]])
        return True

    try:
        added = api["add_admin"](text)
    except Exception:
        PENDING.pop(chat, None)
        api["send"](chat, "Не удалось сохранить администратора. Проверьте права бота на сервере.",
                    [[api["btn"]("👮 К списку", "mod:adm:home")]])
        return True

    PENDING.pop(chat, None)
    if added:
        text_out = "✅ Администратор <code>%s</code> добавлен." % text
    else:
        text_out = "Этот Telegram ID уже есть в списке администраторов."
    api["send"](chat, text_out,
                [[api["btn"]("👮 К списку", "mod:adm:home")],
                 [api["btn"]("⬅️ Меню", "menu")]])
    return True
