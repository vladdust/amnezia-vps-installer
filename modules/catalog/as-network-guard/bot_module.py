"""Telegram screen and controls for the C24Be network-list firewall module."""
import json
import os
from pathlib import Path
import subprocess


MODULE_KEY = "as"
VERSION = "1.0.0"
TITLE = "Сети госорганизаций и VK"
BUTTON = "🛡 Сети РКН / VK"
DESCRIPTION = "Блокировка входящих сетей из C24Be и исходящих сетей VK/Max/OK."

STATE = Path("/var/lib/vpn-bot/modules/as-network-guard/state.json")
HELPER = "/usr/local/lib/vpn-bot/modules/as-network-guard/network_guard.py"


def init(_api):
    return None


def _read_state():
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _helper(*args, timeout=30):
    result = subprocess.run(["/usr/bin/python3", HELPER, *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "неизвестная ошибка")
    return result.stdout.strip()


def _count(value):
    try:
        return format(max(0, int(value)), ",").replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def _active(state):
    return bool(state.get("enabled"))


def status_button(api):
    state = _read_state()
    fresh = max(0, int(state.get("in_total", 0)) - int(state.get("last_seen_in", 0)))
    if state.get("last_error"):
        label = "🔴 Ошибка фильтра"
    elif fresh:
        label = "🔴 Новые попадания: " + _count(fresh)
    elif _active(state):
        label = "🟢 Сети РКН / VK"
    else:
        label = "⚪️ Сети РКН / VK — выключено"
    return [api["btn"](label[:60], "mod:as:home")]


def _home(api, note=""):
    state = _read_state()
    enabled = _active(state)
    lines = ["<b>🛡 Блокировка сетей РКН / VK</b>"]
    if note:
        lines.extend(["", api["e"](note)])
    if state.get("last_error"):
        status = "🔴 ошибка фильтрации"
    elif enabled and (state.get("inbound_enabled") or state.get("outbound_enabled")):
        status = "🟢 работает"
    elif enabled:
        status = "🟡 включена, направления отключены"
    else:
        status = "⚪️ выключена"
    lines.extend(["", "Статус: %s" % status])
    if state.get("inbound_enabled") or state.get("outbound_enabled"):
        lines.append("Входящая фильтрация: %s" % ("включена" if state.get("inbound_enabled") else "выключена"))
        lines.append("Исходящая фильтрация: %s" % ("включена" if state.get("outbound_enabled") else "выключена"))
    lines.extend([
        "",
        "<b>Входящие сети</b>",
        "Заблокировано попыток: %s за сегодня · %s всего" %
        (_count(state.get("in_today")), _count(state.get("in_total"))),
        "Уникальных IP: %s за 24 ч · %s всего" %
        (_count(state.get("in_unique_24h")), _count(state.get("in_unique_total"))),
        "",
        "<b>Исходящие сети VK/Max/OK</b>",
        "Заблокировано подключений: %s за сегодня · %s всего" %
        (_count(state.get("out_today")), _count(state.get("out_total"))),
    ])
    if state.get("lists_updated_at"):
        sources = state.get("list_sources") or {}
        source_text = "кеш" if "кеш" in sources.values() else "C24Be"
        lines.append("\nСписки: %s · источник: %s" % (api["e"](state["lists_updated_at"]), source_text))
    else:
        lines.append("\nСписки ещё не загружены.")
    if state.get("last_error"):
        lines.append("\n⚠️ %s" % api["e"](state["last_error"][:350]))

    main_action = "Остановить" if enabled else "Включить"
    main_callback = "disable" if enabled else "enable"
    keyboard = [
        [api["btn"]("⏹ " + main_action, "mod:as:" + main_callback),
         api["btn"]("🔄 Обновить списки", "mod:as:refresh")],
        [api["btn"]("⚙️ Параметры", "mod:as:settings")],
        [api["btn"]("📊 Состояние сервера", "status")],
        [api["btn"]("⬅️ Меню", "menu")],
    ]
    return "\n".join(lines), keyboard


def _settings(api):
    state = _read_state()
    inbound = bool(state.get("inbound_enabled", True))
    outbound = bool(state.get("outbound_enabled", True))
    keyboard = [
        [api["btn"](("✅ " if inbound else "⬜️ ") + "Входящие сети", "mod:as:toggle-inbound")],
        [api["btn"](("✅ " if outbound else "⬜️ ") + "Исходящие VK/Max/OK", "mod:as:toggle-outbound")],
        [api["btn"]("⬅️ Назад", "mod:as:home")],
    ]
    return "<b>⚙️ Параметры блокировки</b>\n\nНажмите на направление, чтобы включить или выключить его.", keyboard


def handle_callback(api, callback, action):
    message = callback.get("message") or {}
    chat = (message.get("chat") or {}).get("id")
    mid = message.get("message_id")
    if chat is None or mid is None:
        return

    def show(view):
        api["edit"](chat, mid, *view)

    if action == "home":
        try:
            _helper("mark-seen", timeout=15)
        except Exception:
            pass
        show(_home(api))
    elif action == "settings":
        show(_settings(api))
    elif action in ("enable", "disable", "toggle-inbound", "toggle-outbound"):
        state = _read_state()
        try:
            if action == "enable":
                _helper("set", "enabled", timeout=45)
                note = "Блокировка включена."
            elif action == "disable":
                _helper("unset", "enabled", timeout=45)
                note = "Блокировка остановлена."
            elif action == "toggle-inbound":
                target = "unset" if state.get("inbound_enabled", True) else "set"
                _helper(target, "inbound", timeout=45)
                note = "Настройка входящей фильтрации сохранена."
            else:
                target = "unset" if state.get("outbound_enabled", True) else "set"
                _helper(target, "outbound", timeout=45)
                note = "Настройка исходящей фильтрации сохранена."
        except Exception as ex:
            note = "Не удалось применить действие: %s" % str(ex)[:250]
        show(_home(api, note))
    elif action == "refresh":
        try:
            result = subprocess.run(["systemctl", "start", "--no-block", "as-network-guard-refresh.service"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout).strip())
            note = "Обновление списков запущено. Откройте вкладку через несколько секунд, чтобы увидеть результат."
        except Exception as ex:
            note = "Не удалось запустить обновление: %s" % str(ex)[:250]
        show(_home(api, note))
