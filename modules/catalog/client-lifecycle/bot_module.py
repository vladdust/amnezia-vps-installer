#!/usr/bin/env python3
"""Button-driven temporary access and review-only reporting for stale configs."""
import datetime
import json
import os
import re
import tempfile
import threading
import time

MODULE_KEY = "l"
VERSION = "1.3.0"
TITLE = "Сроки доступа и неактивные конфиги"
DESCRIPTION = "Сроки доступа клиентов, продление и проверка неактивных конфигов."
BUTTON = "⏳ Доступы"
STATE_PATH = "/var/lib/vpn-bot/modules/client-lifecycle/state.json"
STALE_AFTER = 30 * 86400
GUEST_DAYS = (1, 7, 30, 90, 365)

_lock = threading.RLock()
_state = {"version": 1, "clients": {}}
_api = None
_pending = {}


def _save_state():
    folder = os.path.dirname(STATE_PATH)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".state.", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(_state, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, STATE_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def init(api):
    global _api, _state
    _api = api
    try:
        with open(STATE_PATH, encoding="utf-8") as stream:
            loaded = json.load(stream)
        if not isinstance(loaded, dict) or not isinstance(loaded.get("clients"), dict):
            raise ValueError("неверный формат состояния")
        _state = loaded
    except FileNotFoundError:
        _state = {"version": 1, "clients": {}}
        _save_state()
    except (OSError, ValueError) as error:
        raise RuntimeError("не удалось безопасно прочитать состояние модуля: %s" % error)


def _date(timestamp):
    return datetime.datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y %H:%M")


def _age(seconds):
    days = max(0, int(seconds // 86400))
    if days:
        return "%d дн." % days
    hours = max(0, int(seconds // 3600))
    return "%d ч." % hours


def _client_match(clients, text):
    key = text.strip()
    matches = [client for client in clients if client["pub"] == key or client["pub"].startswith(key)]
    if len(matches) != 1:
        return None, len(matches)
    return matches[0], 1


def _tracked_clients(api):
    with _lock:
        records = _state.setdefault("clients", {})
        now = int(time.time())
        current = api["get_clients"]()
        active_pubs = set()
        changed = False
        for client in current:
            pub = client["pub"]
            active_pubs.add(pub)
            record = records.get(pub)
            if not isinstance(record, dict):
                record = {"first_seen_at": now, "last_handshake_at": 0}
                records[pub] = record
                changed = True
            hs = int(client.get("hs") or 0)
            if hs > int(record.get("last_handshake_at") or 0):
                record["last_handshake_at"] = hs
                changed = True
        for pub in list(records):
            if pub not in active_pubs:
                del records[pub]
                changed = True
        if changed:
            _save_state()
        return current, records


def _unused(api):
    now = int(time.time())
    clients, records = _tracked_clients(api)
    result = []
    for client in clients:
        record = records.get(client["pub"], {})
        hs = max(int(client.get("hs") or 0), int(record.get("last_handshake_at") or 0))
        if hs:
            age = now - hs
            reason = "последнее подключение %s назад" % _age(age)
        else:
            age = now - int(record.get("first_seen_at") or now)
            reason = "подключений не наблюдалось за %s наблюдения" % _age(age)
        if age >= STALE_AFTER:
            result.append((client, reason, age))
    return sorted(result, key=lambda item: item[2], reverse=True)


def _expiry_view(api):
    clients, records = _tracked_clients(api)
    by_pub = {client["pub"]: client for client in clients}
    now = int(time.time())
    entries = []
    for pub, record in records.items():
        expires = record.get("expires_at")
        if expires and pub in by_pub:
            entries.append((int(expires), by_pub[pub], record))
    entries.sort(key=lambda item: item[0])
    lines = ["<b>⏳ Сроки доступа</b>", ""]
    keyboard = []
    for expires, client, record in entries[:20]:
        status = " · истёк" if expires <= now else ""
        if api["is_blocked"](client["pub"]):
            status += " · отключён"
        lines.append("• <b>%s</b> — до %s%s" % (api["e"](client["name"]), _date(expires), status))
        keyboard.append([api["btn"]("↗ Продлить: " + client["name"][:26], "mod:l:extend:" + client["pub"])])
    if not entries:
        lines.append("Доступов со сроком пока нет.")
    keyboard.extend([
        [api["btn"]("➕ Назначить срок существующему", "mod:l:assign:0")],
        [api["btn"]("➕ Новый клиент", "add")],
        [api["btn"]("🔄 Обновить", "mod:l:expiring"), api["btn"]("⬅️ Клиенты", "clients")],
    ])
    return "\n".join(lines)[:3900], keyboard


def _assign_view(api, page=0):
    clients, records = _tracked_clients(api)
    permanent = [client for client in clients
                 if not records.get(client["pub"], {}).get("expires_at")]
    permanent.sort(key=lambda client: (client.get("name", "").casefold(), client["pub"]))
    page_size = 10
    pages = max(1, (len(permanent) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    selected = permanent[start:start + page_size]
    lines = ["<b>➕ Назначить срок существующему клиенту</b>",
             "Выберите постоянный ключ. После назначения срока доступ отключится автоматически, конфиг сохранится.", ""]
    keyboard = []
    for client in selected:
        status = " · отключён" if api["is_blocked"](client["pub"]) else ""
        lines.append("• <b>%s</b>%s" % (api["e"](client["name"]), status))
        keyboard.append([api["btn"](("⛔ " if status else "👤 ") + client["name"][:36],
                                     "mod:l:choose:" + client["pub"])])
    if not permanent:
        lines.append("Все существующие ключи уже имеют срок доступа.")
    if pages > 1:
        controls = []
        if page > 0:
            controls.append(api["btn"]("⬅️", "mod:l:assign:%d" % (page - 1)))
        controls.append(api["btn"]("%d/%d" % (page + 1, pages), "mod:l:assign:%d" % page))
        if page + 1 < pages:
            controls.append(api["btn"]("➡️", "mod:l:assign:%d" % (page + 1)))
        keyboard.append(controls)
    keyboard.extend([
        [api["btn"]("↩️ К срокам", "mod:l:expiring")],
        [api["btn"]("⬅️ Клиенты", "clients")],
    ])
    return "\n".join(lines)[:3900], keyboard


def _assign_days_keyboard(api, pub):
    return [[api["btn"]("%d дн." % days, "mod:l:ad:%s:%d" % (pub, days))
             for days in GUEST_DAYS[:3]],
            [api["btn"]("%d дн." % days, "mod:l:ad:%s:%d" % (pub, days))
             for days in GUEST_DAYS[3:]],
            [api["btn"]("⬅️ К списку", "mod:l:assign:0")]]


def _assign_access(api, chat, mid, pub, days):
    if days not in GUEST_DAYS:
        api["edit"](chat, mid, "Выбран неверный срок.",
                    [[api["btn"]("↩️ К списку", "mod:l:assign:0")]])
        return
    clients, records = _tracked_clients(api)
    client = api["find_client"](pub)
    if not client:
        api["edit"](chat, mid, "Клиент уже удалён.",
                    [[api["btn"]("↩️ К списку", "mod:l:assign:0")]])
        return
    with _lock:
        record = _state.setdefault("clients", {}).get(client["pub"])
        if not record or record.get("expires_at"):
            api["edit"](chat, mid, "У этого клиента срок уже назначен. Откройте список сроков, чтобы продлить его.",
                        [[api["btn"]("↩️ К срокам", "mod:l:expiring")]])
            return
        expires = int(time.time()) + days * 86400
        record["expires_at"] = expires
        record["disabled_by_module"] = False
        record.pop("expired_at", None)
        _save_state()
    api["edit"](chat, mid,
                "✅ Для <b>%s</b> установлен срок %d дн. Доступ отключится %s, конфиг сохранится." %
                (api["e"](client["name"]), days, _date(expires)),
                [[api["btn"]("⏳ Сроки доступа", "mod:l:expiring"),
                  api["btn"]("👤 Клиент", "c:" + pub)]])


def _unused_view(api, candidates=None):
    candidates = _unused(api) if candidates is None else candidates
    lines = ["<b>🔎 Неактивные конфиги для проверки</b>",
             "Кандидаты не удаляются автоматически. Отключение обратимо.", ""]
    keyboard = []
    for client, reason, _age_sec in candidates[:20]:
        state = " · уже отключён" if api["is_blocked"](client["pub"]) else ""
        lines.append("• <b>%s</b> — %s%s" % (api["e"](client["name"]), api["e"](reason), state))
        if not api["is_blocked"](client["pub"]):
            keyboard.append([api["btn"]("⛔ Проверить отключение: " + client["name"][:26], "mod:l:ask:" + client["pub"])])
    if not candidates:
        lines.append("Пока нет клиентов без активности 30 дней.")
    keyboard.extend([[api["btn"]("🔄 Обновить", "mod:l:unused")],
                     [api["btn"]("⬅️ Клиенты", "clients")]])
    return "\n".join(lines)[:3900], keyboard


def _module_home(api):
    clients, records = _tracked_clients(api)
    guests = [record for record in records.values() if record.get("expires_at")]
    expired = sum(1 for record in guests if int(record["expires_at"]) <= int(time.time()))
    candidates = _unused(api)
    text = ("<b>⏳ Доступы</b>\n\n"
            "Клиентов под наблюдением: %d\n"
            "Доступов со сроком: %d, срок истёк: %d\n"
            "Кандидатов на проверку: %d" % (len(clients), len(guests), expired, len(candidates)))
    keyboard = [[api["btn"]("➕ Новый клиент", "add")],
                [api["btn"]("🕒 Назначить срок существующему", "mod:l:assign:0")],
                [api["btn"]("📅 Сроки доступа", "mod:l:expiring")],
                [api["btn"]("🔎 Неактивные", "mod:l:unused")],
                [api["btn"]("⬅️ Клиенты", "clients")]]
    return text, keyboard


def _guest_days_keyboard(api):
    return [[api["btn"]("%d дн." % days, "mod:l:gd:%d" % days) for days in GUEST_DAYS[:3]],
            [api["btn"]("%d дн." % days, "mod:l:gd:%d" % days) for days in GUEST_DAYS[3:]],
            [api["btn"]("❌ Отмена", "mod:l:cancel")]]


def _extend_days_keyboard(api, pub):
    return [[api["btn"]("%d дн." % days, "mod:l:ed:%s:%d" % (pub, days)) for days in GUEST_DAYS[:3]],
            [api["btn"]("%d дн." % days, "mod:l:ed:%s:%d" % (pub, days)) for days in GUEST_DAYS[3:]],
            [api["btn"]("↩️ К срокам", "mod:l:expiring")]]


def _normalize_name(text):
    return re.sub(r"\s+", " ", text).strip()[:40]


def handle_add_start(api, callback):
    message = callback.get("message") or {}
    chat = (message.get("chat") or {}).get("id")
    mid = message.get("message_id")
    if chat is None or mid is None:
        return False
    api["edit"](chat, mid,
                "<b>➕ Новый клиент</b>\n\nВыберите тип доступа:",
                [[api["btn"]("👤 Обычный", "mod:l:new:regular"),
                  api["btn"]("⏳ Временный", "mod:l:new:temporary")],
                 [api["btn"]("❌ Отмена", "mod:l:cancel")]])
    return True


def _ask_name(api, chat, mid, kind):
    with _lock:
        _pending[chat] = {"kind": kind}
    title = "временного" if kind == "temporary" else "обычного"
    api["edit"](chat, mid,
                "Отправьте имя %s клиента (например, <i>Мама iPhone</i>)." % title,
                [[api["btn"]("❌ Отмена", "mod:l:cancel")]])


def cancel_pending(chat):
    with _lock:
        _pending.pop(chat, None)


def _create_temporary(api, chat, mid, name, days):
    api["edit"](chat, mid, "⏳ Создаю временный доступ на %d дн.…" % days)
    created = None
    try:
        created = api["create_client"](name)
        expires_at = int(time.time()) + days * 86400
        with _lock:
            _state.setdefault("clients", {})[created["pub"]] = {
                "first_seen_at": int(time.time()), "last_handshake_at": 0,
                "expires_at": expires_at, "disabled_by_module": False,
            }
            _save_state()
        api["ag_sync"]()
        api["send_client_conf"](chat, name, created["ip"], created["conf"], created=True)
        api["send"](chat, "⌛ Доступ <b>%s</b> отключится %s. Продлить его можно в разделе «Сроки доступа»." %
                    (api["e"](name), _date(expires_at)),
                    [[api["btn"]("⏳ Сроки доступа", "mod:l:expiring"), api["btn"]("👥 Клиенты", "clients")]])
        api["send"](chat, *api["view_menu"]())
    except Exception as error:
        api["send"](chat, "❌ Не удалось создать временный доступ: %s" % api["e"](str(error)),
                    [[api["btn"]("👥 Клиенты", "clients")]])


def _extend_access(api, chat, mid, pub, days):
    if days not in GUEST_DAYS:
        api["edit"](chat, mid, "Выбран неверный срок.", [[api["btn"]("↩️ К срокам", "mod:l:expiring")]])
        return
    clients, records = _tracked_clients(api)
    client = api["find_client"](pub)
    if not client:
        api["edit"](chat, mid, "Клиент уже удалён.", [[api["btn"]("↩️ К срокам", "mod:l:expiring")]])
        return
    with _lock:
        record = _state.setdefault("clients", {}).get(client["pub"])
        if not record or not record.get("expires_at"):
            api["edit"](chat, mid, "У клиента не задан срок доступа.",
                        [[api["btn"]("↩️ К срокам", "mod:l:expiring")]])
            return
        expires = max(int(time.time()), int(record["expires_at"])) + days * 86400
        if record.get("disabled_by_module") and api["is_blocked"](client["pub"]):
            api["set_blocked"](client["pub"], False)
        record["expires_at"] = expires
        record["disabled_by_module"] = False
        record.pop("expired_at", None)
        _save_state()
    api["edit"](chat, mid, "✅ Доступ <b>%s</b> продлён до %s." %
                (api["e"](client["name"]), _date(expires)),
                [[api["btn"]("↩️ К срокам", "mod:l:expiring"), api["btn"]("👥 Клиенты", "clients")]])


def handle_message(api, message):
    text = (message.get("text") or "").strip()
    chat = (message.get("chat") or {}).get("id")
    if chat is None or not text:
        return False
    with _lock:
        pending = _pending.get(chat)
    if not pending:
        return False
    if text == "/cancel":
        with _lock:
            _pending.pop(chat, None)
        api["send"](chat, "Создание отменено.", [[api["btn"]("👥 Клиенты", "clients")]])
        return True
    if text.startswith("/"):
        with _lock:
            _pending.pop(chat, None)
        return False
    name = _normalize_name(text)
    if not name:
        api["send"](chat, "Введите непустое имя клиента.", [[api["btn"]("❌ Отмена", "mod:l:cancel")]])
        return True
    kind = pending.get("kind")
    if kind == "regular":
        with _lock:
            _pending.pop(chat, None)
        api["do_add"](chat, name)
        return True
    if kind == "temporary":
        with _lock:
            _pending[chat] = {"kind": "duration", "name": name}
        api["send"](chat, "Выберите срок доступа для <b>%s</b>:" % api["e"](name), _guest_days_keyboard(api))
        return True
    return False


def handle_callback(api, callback, action):
    message = callback.get("message") or {}
    chat = (message.get("chat") or {}).get("id")
    mid = message.get("message_id")
    if chat is None or mid is None:
        return

    def show(text, keyboard=None):
        api["edit"](chat, mid, text, keyboard)

    if action == "home":
        show(*_module_home(api))
    elif action == "new:regular":
        _ask_name(api, chat, mid, "regular")
    elif action == "new:temporary":
        _ask_name(api, chat, mid, "temporary")
    elif action == "cancel":
        with _lock:
            _pending.pop(chat, None)
        show("Создание отменено.", [[api["btn"]("👥 Клиенты", "clients")]])
    elif action == "expiring":
        show(*_expiry_view(api))
    elif action.startswith("assign:"):
        value = action[7:]
        if not value.isdigit():
            show("Не удалось открыть список клиентов.", [[api["btn"]("⬅️ Клиенты", "clients")]])
        else:
            show(*_assign_view(api, int(value)))
    elif action.startswith("choose:"):
        pub = action[7:]
        client = api["find_client"](pub)
        if not client:
            show("Клиент уже удалён.", [[api["btn"]("↩️ К списку", "mod:l:assign:0")]])
        else:
            clients, records = _tracked_clients(api)
            record = records.get(pub, {})
            if record.get("expires_at"):
                show("У клиента <b>%s</b> уже задан срок до %s." %
                     (api["e"](client["name"]), _date(int(record["expires_at"]))),
                     [[api["btn"]("↩️ К срокам", "mod:l:expiring")]])
            else:
                show("На сколько дней выдать доступ клиенту <b>%s</b>?" % api["e"](client["name"]),
                     _assign_days_keyboard(api, pub))
    elif action.startswith("ad:"):
        parts = action[3:].rsplit(":", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            show("Не удалось разобрать срок.", [[api["btn"]("↩️ К списку", "mod:l:assign:0")]])
        else:
            _assign_access(api, chat, mid, parts[0], int(parts[1]))
    elif action == "unused":
        show(*_unused_view(api))
    elif action.startswith("extend:"):
        pub = action[7:]
        client = api["find_client"](pub)
        if not client:
            show("Клиент уже удалён.", [[api["btn"]("↩️ К срокам", "mod:l:expiring")]])
        else:
            show("На сколько продлить доступ для <b>%s</b>?" % api["e"](client["name"]),
                 _extend_days_keyboard(api, pub))
    elif action.startswith("ed:"):
        parts = action[3:].rsplit(":", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            show("Не удалось разобрать срок.", [[api["btn"]("↩️ К срокам", "mod:l:expiring")]])
        else:
            _extend_access(api, chat, mid, parts[0], int(parts[1]))
    elif action.startswith("gd:"):
        value = action[3:]
        with _lock:
            pending = _pending.get(chat)
            _pending.pop(chat, None)
        if not value.isdigit() or int(value) not in GUEST_DAYS or not pending or pending.get("kind") != "duration":
            show("Не удалось выбрать срок доступа.", [[api["btn"]("👥 Клиенты", "clients")]])
        else:
            _create_temporary(api, chat, mid, pending["name"], int(value))
    elif action.startswith("ask:"):
        pub = action[4:]
        client = api["find_client"](pub)
        if not client:
            show("Клиент уже удалён.", [[api["btn"]("⬅️ Назад", "mod:l:unused")]])
            return
        show("Отключить <b>%s</b>? Конфиг сохранится, доступ можно вернуть в разделе клиентов." % api["e"](client["name"]),
             [[api["btn"]("✅ Отключить", "mod:l:disable:" + pub), api["btn"]("Отмена", "mod:l:unused")]])
    elif action.startswith("disable:"):
        pub = action[7:]
        client = api["find_client"](pub)
        if not client:
            show("Клиент уже удалён.", [[api["btn"]("⬅️ Назад", "mod:l:unused")]])
            return
        api["set_blocked"](pub, True)
        show("⛔ Клиент <b>%s</b> отключён. Конфиг сохранён." % api["e"](client["name"]),
             [[api["btn"]("⬅️ К неактивным", "mod:l:unused")]])


def tick(api):
    now = int(time.time())
    clients, records = _tracked_clients(api)
    by_pub = {client["pub"]: client for client in clients}
    expired_notifications = []
    with _lock:
        for pub, record in records.items():
            expires_at = int(record.get("expires_at") or 0)
            if not expires_at or expires_at > now or record.get("expired_at"):
                continue
            client = by_pub.get(pub)
            if not client:
                continue
            was_blocked = api["is_blocked"](pub)
            if not was_blocked:
                api["set_blocked"](pub, True)
                record["disabled_by_module"] = True
            else:
                record["disabled_by_module"] = False
            record["expired_at"] = now
            expired_notifications.append(client["name"])
            _save_state()
    for name in expired_notifications:
        api["notify_admins"]("⌛ Срок доступа <b>%s</b> истёк. Клиент отключён, конфиг сохранён." % api["e"](name))


def on_event(api, event, *args):
    if event == "client_rekey" and len(args) == 2:
        old_pub, new_pub = args
        with _lock:
            record = _state.setdefault("clients", {}).pop(old_pub, None)
            if record is not None:
                _state["clients"][new_pub] = record
                _save_state()
    elif event == "client_deleted" and args:
        with _lock:
            if _state.setdefault("clients", {}).pop(args[0], None) is not None:
                _save_state()
