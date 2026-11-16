#!/usr/bin/env python3
"""Patch the installed Amnezia bot with a small, auditable plugin hook layer."""
from pathlib import Path
import os
import sys
import tempfile

MARKER = "# BEGIN BOT MODULE LOADER"
LOADER = r'''# BEGIN BOT MODULE LOADER
BOT_MODULES = {}
BOT_MODULES_DIR = "/opt/vpn-bot/modules"


def _module_api():
    names = ("send", "edit", "btn", "e", "get_clients", "find_client", "create_client", "do_add",
             "delete_client", "set_blocked", "is_blocked", "save_cfg", "send_client_conf",
             "notify_admins", "ag_sync", "view_menu", "time", "os", "json")
    return {name: globals()[name] for name in names if name in globals()}


def _load_bot_modules():
    import importlib.util
    global BOT_MODULES
    loaded = {}
    if not os.path.isdir(BOT_MODULES_DIR):
        BOT_MODULES = loaded
        return
    for module_id in sorted(os.listdir(BOT_MODULES_DIR)):
        module_file = os.path.join(BOT_MODULES_DIR, module_id, "bot_module.py")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,47}", module_id) or not os.path.isfile(module_file):
            continue
        try:
            spec = importlib.util.spec_from_file_location("vpn_bot_module_" + module_id.replace("-", "_"), module_file)
            plugin = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(plugin)
            key = getattr(plugin, "MODULE_KEY", "")
            if not re.fullmatch(r"[a-z0-9]{1,8}", key) or key in loaded:
                raise ValueError("MODULE_KEY is invalid or duplicated")
            init = getattr(plugin, "init", None)
            if init:
                init(_module_api())
            loaded[key] = plugin
            log("loaded bot module:", module_id, getattr(plugin, "VERSION", "?"))
        except Exception:
            log("failed to load bot module %s: %s" % (module_id, traceback.format_exc()))
    BOT_MODULES = loaded


def _module_dashboard():
    if not BOT_MODULES:
        return "<b>🧩 Дополнительные функции</b>\n\nМодули не установлены.", [[btn("⬅️ Меню", "menu")]]
    lines = ["<b>🧩 Дополнительные функции</b>", ""]
    keyboard = []
    api = _module_api()
    for key, plugin in BOT_MODULES.items():
        lines.append("<b>%s</b>\n%s" % (e(getattr(plugin, "TITLE", key)), e(getattr(plugin, "DESCRIPTION", ""))))
        keyboard.append([btn(getattr(plugin, "BUTTON", getattr(plugin, "TITLE", key)), "mod:" + key + ":home")])
    keyboard.append([btn("⬅️ Меню", "menu")])
    return "\n\n".join(lines), keyboard


def _dispatch_module_callback(cq, data, chat, mid):
    if data == "modules":
        edit(chat, mid, *_module_dashboard())
        return True
    api = _module_api()
    if data in ("menu", "clients"):
        for plugin in BOT_MODULES.values():
            reset = getattr(plugin, "cancel_pending", None)
            if reset:
                reset(chat)
    if data == "add":
        for plugin in BOT_MODULES.values():
            handler = getattr(plugin, "handle_add_start", None)
            if handler and handler(api, cq):
                return True
    for key, plugin in BOT_MODULES.items():
        prefix = "mod:" + key + ":"
        if data.startswith(prefix):
            handler = getattr(plugin, "handle_callback", None)
            if handler:
                handler(api, cq, data[len(prefix):])
            return True
    return False


def _dispatch_module_message(message):
    api = _module_api()
    for plugin in BOT_MODULES.values():
        handler = getattr(plugin, "handle_message", None)
        if handler and handler(api, message):
            return True
    return False


def _tick_bot_modules():
    api = _module_api()
    for plugin in BOT_MODULES.values():
        handler = getattr(plugin, "tick", None)
        if handler:
            try:
                handler(api)
            except Exception:
                log("module tick %s: %s" % (getattr(plugin, "MODULE_KEY", "?"), traceback.format_exc()))


def _notify_bot_modules(event, *args):
    api = _module_api()
    for plugin in BOT_MODULES.values():
        handler = getattr(plugin, "on_event", None)
        if handler:
            try:
                handler(api, event, *args)
            except Exception:
                log("module event %s: %s" % (event, traceback.format_exc()))
# END BOT MODULE LOADER
'''


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise SystemExit("Ожидался один участок для изменения (%s), найдено: %d" % (label, count))
    return text.replace(old, new, 1)


def refresh_loader(source):
    if MARKER in source:
        end_marker = "# END BOT MODULE LOADER"
        if source.count(MARKER) != 1 or source.count(end_marker) != 1:
            raise SystemExit("Неоднозначны границы загрузчика модулей.")
        start = source.index(MARKER)
        marker_end = source.index(end_marker, start)
        end = marker_end + len(end_marker)
        if marker_end < start:
            raise SystemExit("Повреждён блок загрузчика модулей.")
        suffix = source[end:].lstrip("\n")
        source = source[:start] + LOADER.rstrip("\n") + "\n\n" + suffix
    else:
        source = replace_once(source, "def handle_callback(cq):\n", LOADER.rstrip("\n") + "\n\ndef handle_callback(cq):\n", "точка загрузчика")

    callback_hook = ('    try:\n        if _dispatch_module_callback(cq, data, chat, mid):\n'
                     '            return\n        if data == "menu":')
    callback_anchor = '    try:\n        if data == "menu":'
    if callback_hook not in source:
        source = replace_once(source, callback_anchor, callback_hook, "обработчик callback")

    message_hook = ('    if _dispatch_module_message(m):\n        return\n\n'
                    '    if pending.get(chat) == "add_name" and text and not text.startswith("/"):' )
    message_anchor = '    if pending.get(chat) == "add_name" and text and not text.startswith("/"):'
    if message_hook not in source:
        source = replace_once(source, message_anchor, message_hook, "обработчик сообщений")
    return source


def remove_module_commands(source):
    lines = (
        '        {"command": "guest", "description": "Временный клиент: /guest дни имя"},\n',
        '        {"command": "expiring", "description": "Сроки временного доступа"},\n',
        '        {"command": "unused", "description": "Проверить неактивные конфиги"},\n',
        '        {"command": "extend", "description": "Продлить доступ клиента"},\n',
    )
    for line in lines:
        source = source.replace(line, "")
    return source


def update_client_menu(source):
    old_main_row = '           [btn("🧩 Доп. функции", "modules")],\n'
    if old_main_row in source:
        if source.count(old_main_row) != 1:
            raise SystemExit("Неоднозначна старая кнопка «Доп. функции».")
        source = source.replace(old_main_row, "", 1)

    anchor = '    kb.append([btn("➕ Новый клиент", "add"), btn("🔄 Обновить", "clients")])'
    row = ('    if "l" in BOT_MODULES:\n'
           '        kb.append([btn("⏳ Доступы", "mod:l:home"), btn("🔎 Неактивные", "mod:l:unused")])\n')
    if row not in source:
        source = replace_once(source, anchor, row + anchor, "кнопки в меню клиентов")
    return source


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: patch-bot-loader.py /opt/vpn-bot/bot.py")
    path = Path(sys.argv[1])
    source = path.read_text(encoding="utf-8")
    source = refresh_loader(source)
    source = remove_module_commands(source)
    if MARKER not in path.read_text(encoding="utf-8"):
        source = replace_once(source,
            '        if alerts:\n            notify_admins("\\n".join(alerts))',
            '        _tick_bot_modules()\n        if alerts:\n            notify_admins("\\n".join(alerts))', "планировщик модулей")
        source = replace_once(source,
            '    ag_state_repub(ip, newpub)   # чтобы синхронизация AdGuard не сбросила настройки (детский режим)\n    return {"pub": newpub, "ip": ip, "conf": text}',
            '    ag_state_repub(ip, newpub)   # чтобы синхронизация AdGuard не сбросила настройки (детский режим)\n    _notify_bot_modules("client_rekey", pub, newpub)\n    return {"pub": newpub, "ip": ip, "conf": text}', "перевыпуск ключа")
        source = replace_once(source,
            '\n\ndef qr_png(text):',
            '\n    _notify_bot_modules("client_deleted", pub)\n\n\ndef qr_png(text):', "удаление клиента")
        source = replace_once(source,
            'def main():\n    if not CFG.get("token"):',
            'def main():\n    _load_bot_modules()\n    if not CFG.get("token"):', "запуск бота")

    source = update_client_menu(source)
    if source == path.read_text(encoding="utf-8"):
        print("Кнопки функций уже находятся в меню клиентов.")
        return

    fd, temporary = tempfile.mkstemp(prefix=".bot.py.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(source)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.chown(temporary, path.stat().st_uid, path.stat().st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("Меню функций клиентов обновлено в %s" % path)


if __name__ == "__main__":
    main()
