#!/usr/bin/env python3
"""Manage C24Be network lists and packet counters for an AmneziaWG host."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request


STATE_DIR = Path("/var/lib/vpn-bot/modules/as-network-guard")
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "state.lock"
CACHE_DIR = STATE_DIR / "lists"
BOT_CONFIG = Path("/etc/vpn-bot/config.json")
MODULE_DIR = Path("/usr/local/lib/vpn-bot/modules/as-network-guard")
MAX_LIST_BYTES = 4 * 1024 * 1024
MAX_SCAN_IPS = 262144
DAILY_TIMEOUT = 86400

LISTS = {
    "gov4": {
        "url": "https://raw.githubusercontent.com/C24Be/AS_Network_List/main/blacklists_iptables/blacklist-v4.ipset",
        "family": "inet", "set": "vbas_gov4",
    },
    "gov6": {
        "url": "https://raw.githubusercontent.com/C24Be/AS_Network_List/main/blacklists_iptables/blacklist-v6.ipset",
        "family": "inet6", "set": "vbas_gov6",
    },
    "vk4": {
        "url": "https://raw.githubusercontent.com/C24Be/AS_Network_List/main/blacklists_iptables/blacklist-vk-v4.ipset",
        "family": "inet", "set": "vbas_vk4",
    },
    "vk6": {
        "url": "https://raw.githubusercontent.com/C24Be/AS_Network_List/main/blacklists_iptables/blacklist-vk-v6.ipset",
        "family": "inet6", "set": "vbas_vk6",
    },
}
TOOLS = {
    "inet": {"fw": "iptables", "gov": "vbas_gov4", "vk": "vbas_vk4",
             "all": "vbas_scan_all4", "day": "vbas_scan_day4"},
    "inet6": {"fw": "ip6tables", "gov": "vbas_gov6", "vk": "vbas_vk6",
              "all": "vbas_scan_all6", "day": "vbas_scan_day6"},
}
IN_CHAIN = "VPNBOT-AS-IN"
OUT_CHAIN = "VPNBOT-AS-OUT"


def now_local():
    return datetime.now().astimezone()


def default_state():
    return {
        "enabled": False,
        "inbound_enabled": True,
        "outbound_enabled": True,
        "lists_ready": False,
        "lists_updated_at": None,
        "list_sources": {},
        "list_counts": {},
        "last_error": "",
        "day": now_local().date().isoformat(),
        "in_today": 0,
        "out_today": 0,
        "in_total": 0,
        "out_total": 0,
        "in_unique_24h": 0,
        "in_unique_total": 0,
        "last_rule_counts": {"in": 0, "out": 0},
        "last_seen_in": 0,
        "vpn_sources": {},
        "wan4": "",
        "wan6": "",
    }


def ensure_dirs():
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    os.chmod(CACHE_DIR, 0o700)


@contextmanager
def state_lock():
    ensure_dirs()
    with LOCK_FILE.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


def load_state():
    state = default_state()
    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            raise ValueError("state.json должен содержать объект")
        state.update(saved)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as ex:
        raise RuntimeError("Не удалось прочитать состояние модуля: %s" % ex)
    return state


def save_state(state):
    ensure_dirs()
    fd, temporary = tempfile.mkstemp(prefix=".state.", dir=str(STATE_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, STATE_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def call(args, check=True, input_text=None, timeout=30):
    try:
        result = subprocess.run(args, input=input_text, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as ex:
        if check:
            raise RuntimeError("Не удалось выполнить %s: %s" % (args[0], ex))
        return None
    if check and result.returncode:
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError("%s: %s" % (" ".join(args[:3]), message or "код %d" % result.returncode))
    return result


def parse_network_list(raw, family):
    text = raw.decode("utf-8", "strict") if isinstance(raw, bytes) else raw
    networks = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[0] != "add":
            continue
        try:
            network = ipaddress.ip_network(fields[2], strict=False)
        except ValueError:
            continue
        if (network.version == 4) != (family == "inet"):
            continue
        networks.add(str(network))
    if not networks:
        raise ValueError("список не содержит корректных IPv%s сетей" % ("4" if family == "inet" else "6"))
    return sorted(networks, key=lambda item: (ipaddress.ip_network(item).network_address,
                                               ipaddress.ip_network(item).prefixlen))


def fetch_list(name, spec):
    request = urllib.request.Request(spec["url"], headers={"User-Agent": "amnezia-vps-installer/1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(MAX_LIST_BYTES + 1)
    if len(raw) > MAX_LIST_BYTES:
        raise ValueError("список превысил 4 МБ")
    networks = parse_network_list(raw, spec["family"])
    return raw, networks


def ipset_exists(name):
    result = call(["ipset", "list", name], check=False)
    return result is not None and result.returncode == 0


def create_set(name, kind, family, timeout=None):
    args = ["ipset", "create", "-exist", name, kind, "family", family,
            "maxelem", str(MAX_SCAN_IPS if kind == "hash:ip" else 131072)]
    if timeout is not None:
        args += ["timeout", str(timeout)]
    args.append("-exist")
    call(args)


def replace_network_set(name, family, networks):
    temporary = name + "_new"
    if not ipset_exists(name):
        create_set(name, "hash:net", family)
    call(["ipset", "destroy", temporary], check=False)
    create_set(temporary, "hash:net", family)
    payload = "".join("add %s %s\n" % (temporary, network) for network in networks)
    try:
        call(["ipset", "restore", "-exist"], input_text=payload, timeout=60)
        call(["ipset", "swap", temporary, name])
    finally:
        call(["ipset", "destroy", temporary], check=False)


def restore_tracker_set(name, family, timeout=None):
    if ipset_exists(name):
        return
    create_set(name, "hash:ip", family, timeout)
    saved = STATE_DIR / (name + ".save")
    if saved.is_file():
        raw = saved.read_text(encoding="utf-8")
        if len(raw) <= 32 * 1024 * 1024:
            call(["ipset", "restore", "-exist"], input_text=raw, timeout=120)


def save_tracker_set(name):
    result = call(["ipset", "save", name], check=False)
    if result is None or result.returncode:
        return
    ensure_dirs()
    path = STATE_DIR / (name + ".save")
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == result.stdout:
                return
        except OSError:
            pass
    fd, temporary = tempfile.mkstemp(prefix="." + name + ".", dir=str(STATE_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(result.stdout)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def tracker_count(name):
    result = call(["ipset", "list", "-t", name], check=False)
    if result is None or result.returncode:
        return 0
    for line in result.stdout.splitlines():
        if line.startswith("Number of entries:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return 0
    return 0


def refresh_lists(state):
    raw_by_name = {}
    networks_by_name = {}
    sources = {}
    errors = []
    for name, spec in LISTS.items():
        cache = CACHE_DIR / (name + ".ipset")
        try:
            raw, networks = fetch_list(name, spec)
            sources[name] = "онлайн"
        except Exception as ex:
            if not cache.is_file():
                errors.append("%s: %s" % (name, ex))
                continue
            raw = cache.read_bytes()
            try:
                networks = parse_network_list(raw, spec["family"])
            except Exception as cache_ex:
                errors.append("%s: источник и кеш недоступны (%s; %s)" % (name, ex, cache_ex))
                continue
            sources[name] = "кеш"
        raw_by_name[name] = raw
        networks_by_name[name] = networks
    if errors:
        state["last_error"] = "; ".join(errors)
        save_state(state)
        return False

    staged = []
    try:
        for name, spec in LISTS.items():
            target = spec["set"]
            temporary = target + "_new"
            if not ipset_exists(target):
                create_set(target, "hash:net", spec["family"])
            call(["ipset", "destroy", temporary], check=False)
            create_set(temporary, "hash:net", spec["family"])
            payload = "".join("add %s %s\n" % (temporary, network)
                              for network in networks_by_name[name])
            call(["ipset", "restore", "-exist"], input_text=payload, timeout=60)
            staged.append((temporary, target, spec["family"]))
        for temporary, target, _family in staged:
            call(["ipset", "swap", temporary, target])
        for temporary, _target, _family in staged:
            call(["ipset", "destroy", temporary], check=False)
    except Exception:
        for temporary, _target, _family in staged:
            call(["ipset", "destroy", temporary], check=False)
        raise

    for name, raw in raw_by_name.items():
        path = CACHE_DIR / (name + ".ipset")
        fd, temporary = tempfile.mkstemp(prefix="." + name + ".", dir=str(CACHE_DIR))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    state["lists_ready"] = True
    state["lists_updated_at"] = now_local().isoformat(timespec="minutes")
    state["list_sources"] = sources
    state["list_counts"] = {name: len(networks_by_name[name]) for name in networks_by_name}
    state["last_error"] = ""
    save_state(state)
    return True


def ensure_tracker_sets():
    for family, names in TOOLS.items():
        restore_tracker_set(names["all"], family)
        restore_tracker_set(names["day"], family, DAILY_TIMEOUT)


def restore_policy_sets_from_cache():
    for name, spec in LISTS.items():
        target = spec["set"]
        if ipset_exists(target):
            continue
        cache = CACHE_DIR / (name + ".ipset")
        if not cache.is_file():
            raise RuntimeError("Не найдена сохранённая копия списка %s" % name)
        try:
            networks = parse_network_list(cache.read_bytes(), spec["family"])
        except Exception as ex:
            raise RuntimeError("Сохранённый список %s повреждён: %s" % (name, ex))
        replace_network_set(target, spec["family"], networks)


def chain_exists(tool, chain):
    result = call([tool, "-S", chain], check=False)
    return result is not None and result.returncode == 0


def ensure_chain(tool, chain):
    if not chain_exists(tool, chain):
        call([tool, "-N", chain])
        return True
    return False


def ensure_rule(tool, chain, rule):
    if call([tool, "-C", chain] + rule, check=False).returncode:
        call([tool, "-A", chain] + rule)


def attach_hook(tool, parent, chain):
    if not chain_exists(tool, parent):
        return False
    if call([tool, "-C", parent, "-j", chain], check=False).returncode:
        call([tool, "-I", parent, "1", "-j", chain])
    return True


def remove_hook(tool, parent, chain):
    if not chain_exists(tool, parent):
        return
    while call([tool, "-D", parent, "-j", chain], check=False).returncode == 0:
        pass


def firewall_tools():
    found = {}
    for family, names in TOOLS.items():
        if shutil.which(names["fw"]):
            found[family] = names
    return found


def get_default_interface(version):
    result = call(["ip", "-%d" % version, "route", "show", "default"], check=False)
    if result is None or result.returncode:
        return ""
    for line in result.stdout.splitlines():
        fields = line.split()
        if "dev" in fields:
            return fields[fields.index("dev") + 1]
    return ""


def get_vpn_sources():
    try:
        config = json.loads(BOT_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"inet": [], "inet6": []}
    container = str(config.get("awg_container", ""))
    interface = str(config.get("awg_if", ""))
    if not container or not interface:
        return {"inet": [], "inet6": []}
    result = {"inet": [], "inet6": []}
    inspect = call(["docker", "inspect", "-f",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", container], check=False)
    if inspect and inspect.returncode == 0:
        for value in inspect.stdout.split():
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            result["inet" if address.version == 4 else "inet6"].append(str(address))
    for version, family in ((4, "inet"), (6, "inet6")):
        routes = call(["docker", "exec", container, "ip", "-%d" % version, "route", "show",
                       "dev", interface, "proto", "kernel"], check=False, timeout=15)
        if routes is None or routes.returncode:
            continue
        for line in routes.stdout.splitlines():
            token = line.split()[0] if line.split() else ""
            try:
                network = ipaddress.ip_network(token, strict=False)
            except ValueError:
                continue
            if (network.version == 4) == (family == "inet"):
                result[family].append(str(network))
    return {family: sorted(set(values)) for family, values in result.items()}


def set_counter_rules(tool, chain, gov_set, all_set, day_set):
    base = ["-m", "conntrack", "--ctstate", "NEW",
            "-m", "set", "--match-set", gov_set, "src"]
    ensure_rule(tool, chain, base + ["-j", "SET", "--add-set", all_set, "src", "--exist"])
    ensure_rule(tool, chain, base + ["-j", "SET", "--add-set", day_set, "src",
                                    "--timeout", str(DAILY_TIMEOUT), "--exist"])
    ensure_rule(tool, chain, base + ["-j", "DROP"])


def setup_inbound(toolset):
    hooks = []
    for family, names in toolset.items():
        tool = names["fw"]
        ensure_chain(tool, IN_CHAIN)
        set_counter_rules(tool, IN_CHAIN, names["gov"], names["all"], names["day"])
        if attach_hook(tool, "INPUT", IN_CHAIN):
            hooks.append((tool, "INPUT"))
        parent = "DOCKER-USER" if chain_exists(tool, "DOCKER-USER") else "FORWARD"
        if attach_hook(tool, parent, IN_CHAIN):
            hooks.append((tool, parent))
    return hooks


def setup_outbound(toolset, sources, wan4, wan6, state):
    signatures = {family: values for family, values in sources.items()}
    signatures["wan4"] = wan4
    signatures["wan6"] = wan6
    old_signatures = state.get("vpn_sources", {})
    new_rules = old_signatures != signatures
    hooks = []
    for family, names in toolset.items():
        tool = names["fw"]
        created = ensure_chain(tool, OUT_CHAIN)
        if created or new_rules:
            call([tool, "-F", OUT_CHAIN])
            output_interface = wan4 if family == "inet" else wan6
            if output_interface:
                for source in sources.get(family, []):
                    rule = ["-o", output_interface, "-s", source, "-m", "set",
                            "--match-set", names["vk"], "dst", "-m", "conntrack",
                            "--ctstate", "NEW", "-j", "REJECT"]
                    call([tool, "-A", OUT_CHAIN] + rule)
        parent = "DOCKER-USER" if chain_exists(tool, "DOCKER-USER") else "FORWARD"
        if attach_hook(tool, parent, OUT_CHAIN):
            hooks.append((tool, parent))
    state["vpn_sources"] = signatures
    if new_rules:
        state.setdefault("last_rule_counts", {})["out"] = 0
    return hooks


def remove_all_rules(delete_sets=False):
    for family, names in TOOLS.items():
        tool = names["fw"]
        if not shutil.which(tool):
            continue
        for parent in ("INPUT", "DOCKER-USER", "FORWARD"):
            remove_hook(tool, parent, IN_CHAIN)
            remove_hook(tool, parent, OUT_CHAIN)
        for chain in (IN_CHAIN, OUT_CHAIN):
            if chain_exists(tool, chain):
                call([tool, "-F", chain], check=False)
                call([tool, "-X", chain], check=False)
    if delete_sets:
        for family, names in TOOLS.items():
            for name in (names["gov"], names["vk"]):
                call(["ipset", "destroy", name], check=False)


def drop_counter(tool, chain):
    if not chain_exists(tool, chain):
        return 0
    result = call([tool, "-L", chain, "-n", "-v", "-x"], check=False)
    if result is None or result.returncode:
        return 0
    total = 0
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[2] in ("DROP", "REJECT"):
            try:
                total += int(fields[0])
            except ValueError:
                pass
    return total


def snapshot_locked(state):
    day = now_local().date().isoformat()
    if state.get("day") != day:
        state["day"] = day
        state["in_today"] = 0
        state["out_today"] = 0
    counters = {"in": 0, "out": 0}
    for family, names in firewall_tools().items():
        counters["in"] += drop_counter(names["fw"], IN_CHAIN)
        counters["out"] += drop_counter(names["fw"], OUT_CHAIN)
    previous = state.setdefault("last_rule_counts", {"in": 0, "out": 0})
    for name, current in counters.items():
        old = int(previous.get(name, 0) or 0)
        delta = current - old if current >= old else current
        if name == "in":
            state["in_total"] = int(state.get("in_total", 0)) + delta
            state["in_today"] = int(state.get("in_today", 0)) + delta
        else:
            state["out_total"] = int(state.get("out_total", 0)) + delta
            state["out_today"] = int(state.get("out_today", 0)) + delta
        previous[name] = current
    state["in_unique_24h"] = sum(tracker_count(names["day"]) for names in TOOLS.values())
    state["in_unique_total"] = sum(tracker_count(names["all"]) for names in TOOLS.values())
    state["last_snapshot_at"] = now_local().isoformat(timespec="minutes")
    for names in TOOLS.values():
        save_tracker_set(names["all"])
        save_tracker_set(names["day"])
    save_state(state)
    return state


def reset_counter_baselines(state):
    state["last_rule_counts"] = {"in": 0, "out": 0}


def snapshot():
    with state_lock():
        state = load_state()
        return snapshot_locked(state)


def _apply_locked(state):
    ensure_tracker_sets()
    snapshot_locked(state)
    if not state.get("enabled"):
        remove_all_rules(delete_sets=False)
        reset_counter_baselines(state)
        save_state(state)
        return
    if not state.get("lists_ready"):
        raise RuntimeError("Списки ещё не загружены. Нажмите «Обновить списки» и попробуйте снова.")
    restore_policy_sets_from_cache()
    tools = firewall_tools()
    if not tools:
        raise RuntimeError("Не найдены iptables/ip6tables.")
    if state.get("inbound_enabled"):
        state["active_in_hooks"] = setup_inbound(tools)
    else:
        for family, names in tools.items():
            tool = names["fw"]
            for parent in ("INPUT", "DOCKER-USER", "FORWARD"):
                remove_hook(tool, parent, IN_CHAIN)
    if state.get("outbound_enabled"):
        sources = get_vpn_sources()
        wan4 = get_default_interface(4)
        wan6 = get_default_interface(6)
        state["wan4"] = wan4
        state["wan6"] = wan6
        state["active_out_hooks"] = setup_outbound(tools, sources, wan4, wan6, state)
        if not any(sources.values()):
            state["last_error"] = "Контейнер AmneziaWG или его VPN-подсеть пока не найдены; исходящая фильтрация ожидает контейнер."
        else:
            state["last_error"] = ""
    else:
        for family, names in tools.items():
            tool = names["fw"]
            for parent in ("DOCKER-USER", "FORWARD"):
                remove_hook(tool, parent, OUT_CHAIN)
    save_state(state)


def apply():
    with state_lock():
        state = load_state()
        try:
            _apply_locked(state)
        except Exception as ex:
            remove_all_rules(delete_sets=False)
            reset_counter_baselines(state)
            state["last_error"] = str(ex)
            save_state(state)
            raise


def remove():
    with state_lock():
        state = load_state()
        snapshot_locked(state)
        remove_all_rules(delete_sets=True)
        reset_counter_baselines(state)
        for names in TOOLS.values():
            save_tracker_set(names["all"])
            save_tracker_set(names["day"])
            call(["ipset", "destroy", names["all"]], check=False)
            call(["ipset", "destroy", names["day"]], check=False)
        save_state(state)


def set_option(option, value):
    with state_lock():
        state = load_state()
        snapshot_locked(state)
        if option == "enabled":
            if value and not state.get("lists_ready"):
                raise RuntimeError("Списки ещё не загружены. Сначала нажмите «Обновить списки».")
            state["enabled"] = value
        elif option == "inbound":
            state["inbound_enabled"] = value
        elif option == "outbound":
            state["outbound_enabled"] = value
        else:
            raise RuntimeError("Неизвестная настройка")
        state["last_error"] = ""
        save_state(state)
        if state.get("enabled"):
            # apply() acquires the lock itself; rules are applied after this transaction.
            pass
        else:
            remove_all_rules(delete_sets=False)
            reset_counter_baselines(state)
    if state.get("enabled"):
        apply()


def mark_seen():
    with state_lock():
        state = load_state()
        snapshot_locked(state)
        state["last_seen_in"] = int(state.get("in_total", 0))
        save_state(state)


def command_refresh():
    with state_lock():
        state = load_state()
        snapshot_locked(state)
        if not refresh_lists(state):
            save_state(state)
            raise RuntimeError(state.get("last_error") or "Не удалось обновить списки.")
        state = load_state()
        if state.get("enabled"):
            # Reapply so new sets are attached to the active rules.
            pass
    if state.get("enabled"):
        apply()


def status():
    state = snapshot()
    print(json.dumps(state, ensure_ascii=False))


def main(argv):
    actions = {
        "apply": apply,
        "snapshot": snapshot,
        "refresh": command_refresh,
        "remove": remove,
        "mark-seen": mark_seen,
    }
    if len(argv) == 2 and argv[1] in actions:
        actions[argv[1]]()
        return
    if len(argv) == 3 and argv[1] == "set" and argv[2] in ("enabled", "inbound", "outbound"):
        set_option(argv[2], True)
        return
    if len(argv) == 3 and argv[1] == "unset" and argv[2] in ("enabled", "inbound", "outbound"):
        set_option(argv[2], False)
        return
    if len(argv) == 2 and argv[1] == "status":
        status()
        return
    raise SystemExit("Использование: network_guard.py {apply|snapshot|refresh|remove|mark-seen|status|set|unset}")


if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception as error:
        print("ОШИБКА: %s" % error, file=sys.stderr)
        raise SystemExit(1)
