#!/usr/bin/env bash
set -Eeuo pipefail
set +x

# Add the modular bot loader and bot modules to an existing Amnezia Server Kit
# installation. The network filter is installed stopped and changes no firewall
# rules until an administrator enables it in the bot.

BOT="/opt/vpn-bot/bot.py"
CONFIG="/etc/vpn-bot/config.json"
SERVICE="vpn-bot.service"
REPOSITORY="vladdust/amnezia-vps-installer"
BRANCH="main"
TMP_DIR=""
BACKUP=""

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
cleanup() {
    [ -z "$TMP_DIR" ] || rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || die "Запусти эту команду от root: curl ... | sudo bash"
command -v curl >/dev/null 2>&1 || die "Не найден curl"
command -v python3 >/dev/null 2>&1 || die "Не найден python3"
command -v systemctl >/dev/null 2>&1 || die "Не найден systemctl"

[ -f "$BOT" ] || die "Не найден $BOT. Скрипт рассчитан на установленный Amnezia Server Kit."
[ ! -L "$BOT" ] || die "$BOT является символической ссылкой; остановка без изменений."
[ -f "$CONFIG" ] || die "Не найден $CONFIG. Остановка без изменений."
systemctl cat "$SERVICE" >/dev/null 2>&1 || die "Не найдена служба $SERVICE. Остановка без изменений."
systemctl is-active --quiet "$SERVICE" || die "Служба $SERVICE не запущена. Сначала проверь её состояние."

if command -v vpn-bot-module >/dev/null 2>&1; then
    cat <<'EOF'
Модульный менеджер уже установлен. Для обновления используй:
  sudo vpn-bot-module refresh
  sudo vpn-bot-module list
  sudo vpn-bot-module upgrade client-lifecycle

Если модуль ещё не установлен, вместо upgrade выполни:
  sudo vpn-bot-module install client-lifecycle

Сетевой фильтр и его вкладку в разделе «Состояние» установи отдельным скриптом:
  curl -fsSL https://raw.githubusercontent.com/vladdust/amnezia-vps-installer/main/install-network-guard.sh | sudo bash
EOF
    exit 0
fi

[ ! -e /usr/local/sbin/vpn-bot-module ] || die "Файл /usr/local/sbin/vpn-bot-module уже существует, но не доступен в PATH. Остановка без изменений."
[ ! -e /opt/vpn-bot/module-catalog/client-lifecycle ] || die "Каталог client-lifecycle уже существует. Проверь установку вручную; файлы не перезаписаны."
[ ! -e /opt/vpn-bot/modules/client-lifecycle ] || die "Каталог установленного модуля уже существует. Остановка без изменений."

TMP_DIR=$(mktemp -d /tmp/vpn-bot-module-setup.XXXXXX)
chmod 0700 "$TMP_DIR"
ARCHIVE="$TMP_DIR/source.zip"
SOURCE="$TMP_DIR/source"
URL="https://codeload.github.com/$REPOSITORY/zip/refs/heads/$BRANCH"

printf 'Загружаю только менеджер и модуль бота из %s…\n' "$REPOSITORY"
curl --fail --silent --show-error --location --retry 3 --proto '=https' --tlsv1.2 "$URL" -o "$ARCHIVE" ||
    die "Не удалось загрузить репозиторий. Серверные файлы пока не изменены."

PACKAGE_ARCHIVE="$TMP_DIR/installer-package.zip"
python3 - "$ARCHIVE" "$PACKAGE_ARCHIVE" <<'PY'
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import zipfile

archive, destination = Path(sys.argv[1]), Path(sys.argv[2])
root_names = set()
found = False
with zipfile.ZipFile(archive) as zipped:
    for item in zipped.infolist():
        path = PurePosixPath(item.filename)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise SystemExit("В репозитории найден небезопасный путь")
        root_names.add(path.parts[0])
        if stat.S_ISLNK(item.external_attr >> 16):
            raise SystemExit("Архив репозитория содержит символическую ссылку")
        if len(path.parts) == 2 and path.parts[1] == "installer-package.zip" and not item.is_dir():
            if found or item.file_size > 5 * 1024 * 1024:
                raise SystemExit("Вложенный пакет повторяется или превышает 5 МБ")
            with zipped.open(item) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
            found = True
if len(root_names) != 1:
    raise SystemExit("Неожиданная структура архива GitHub")
if not found:
    raise SystemExit("В GitHub-архиве не найден installer-package.zip")
PY

mkdir -m 0700 "$SOURCE"
python3 - "$PACKAGE_ARCHIVE" "$SOURCE" <<'PY'
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import zipfile

archive, destination = Path(sys.argv[1]), Path(sys.argv[2])
allowed = {
    "modules/vpn-bot-module",
    "modules/patch-bot-loader.py",
    "modules/catalog/client-lifecycle/manifest.json",
    "modules/catalog/client-lifecycle/bot_module.py",
    "modules/catalog/as-network-guard/manifest.json",
    "modules/catalog/as-network-guard/bot_module.py",
    "modules/catalog/as-network-guard/network_guard.py",
    "modules/catalog/as-network-guard/as-network-guard.service",
    "modules/catalog/as-network-guard/as-network-guard-refresh.service",
    "modules/catalog/as-network-guard/as-network-guard-refresh.timer",
    "modules/catalog/as-network-guard/as-network-guard-snapshot.service",
    "modules/catalog/as-network-guard/as-network-guard-snapshot.timer",
}
found = set()
expanded = 0
with zipfile.ZipFile(archive) as zipped:
    for item in zipped.infolist():
        path = PurePosixPath(item.filename)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise SystemExit("Архив содержит небезопасный путь")
        mode = item.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise SystemExit("Архив содержит символическую ссылку")
        name = path.as_posix()
        if name not in allowed or item.is_dir():
            continue
        if name in found:
            raise SystemExit("В пакете повторяется файл " + name)
        expanded += item.file_size
        if expanded > 5 * 1024 * 1024:
            raise SystemExit("Размер выбранных файлов превышает допустимый предел")
        target = destination.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipped.open(item) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        found.add(name)
missing = sorted(allowed - found)
if missing:
    raise SystemExit("В архиве не найдены необходимые файлы: " + ", ".join(missing))
PY

PATCHER="$SOURCE/modules/patch-bot-loader.py"
MANAGER="$SOURCE/modules/vpn-bot-module"
CATALOG="$SOURCE/modules/catalog/client-lifecycle"
SCAN_CATALOG="$SOURCE/modules/catalog/as-network-guard"
python3 -m py_compile "$PATCHER" "$MANAGER" "$CATALOG/bot_module.py" "$SCAN_CATALOG/bot_module.py" "$SCAN_CATALOG/network_guard.py" || die "Проверка Python-файлов не пройдена. Сервер не изменён."

# Preflight the exact bot patch on a temporary copy. The patcher refuses to
# write unless every expected insertion point is present exactly once.
cp -p "$BOT" "$TMP_DIR/bot.preflight.py"
python3 "$PATCHER" "$TMP_DIR/bot.preflight.py" || die "Эта версия bot.py не совпала с ожидаемыми точками расширения. Сервер не изменён."
python3 -m py_compile "$TMP_DIR/bot.preflight.py" || die "Синтаксическая проверка изменённого бота не пройдена. Сервер не изменён."

STAMP=$(date -u '+%Y%m%dT%H%M%SZ')
BOT_MODE=$(stat -c '%a' "$BOT")
BOT_UID=$(stat -c '%u' "$BOT")
BOT_GID=$(stat -c '%g' "$BOT")
BACKUP_DIR="/var/backups/vpn-bot-module-migration/$STAMP"
install -d -o root -g root -m 0700 "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/bot.py"
cp -p "$BOT" "$BACKUP"
chmod 0600 "$BACKUP"

install -o root -g root -m 0750 "$MANAGER" /usr/local/sbin/vpn-bot-module
install -d -o root -g root -m 0750 /opt/vpn-bot/module-catalog/client-lifecycle
install -o root -g root -m 0640 "$CATALOG/manifest.json" /opt/vpn-bot/module-catalog/client-lifecycle/manifest.json
install -o root -g root -m 0640 "$CATALOG/bot_module.py" /opt/vpn-bot/module-catalog/client-lifecycle/bot_module.py
install -d -o root -g root -m 0750 /opt/vpn-bot/module-catalog/as-network-guard
cp -a "$SCAN_CATALOG/." /opt/vpn-bot/module-catalog/as-network-guard/
chown -R root:root /opt/vpn-bot/module-catalog/as-network-guard
find /opt/vpn-bot/module-catalog/as-network-guard -type d -exec chmod 0750 {} +
find /opt/vpn-bot/module-catalog/as-network-guard -type f -exec chmod 0640 {} +

if ! python3 "$PATCHER" "$BOT"; then
    install -o "$BOT_UID" -g "$BOT_GID" -m "$BOT_MODE" "$BACKUP" "$BOT"
    die "Не удалось изменить bot.py. Оригинал восстановлен из $BACKUP"
fi

if ! /usr/local/sbin/vpn-bot-module install client-lifecycle; then
    install -o "$BOT_UID" -g "$BOT_GID" -m "$BOT_MODE" "$BACKUP" "$BOT"
    systemctl restart "$SERVICE" || true
    die "Модуль не установился. Оригинальный bot.py восстановлен; менеджер оставлен для диагностики. Резервная копия: $BACKUP"
fi

if ! /usr/local/sbin/vpn-bot-module install as-network-guard; then
    install -o "$BOT_UID" -g "$BOT_GID" -m "$BOT_MODE" "$BACKUP" "$BOT"
    systemctl restart "$SERVICE" || true
    die "Сетевой модуль не установился. bot.py восстановлен; резервная копия: $BACKUP"
fi

printf '\nГотово. Установлены менеджер модулей, управление сроками и вкладка сетевого фильтра.\n'
printf 'Фильтр в разделе «Состояние» выключен до отдельного нажатия кнопки включения.\n'
printf 'Резервная копия bot.py: %s\n' "$BACKUP"
printf 'Проверка: sudo vpn-bot-module list\n'
printf 'Для будущих функций: sudo vpn-bot-module refresh, затем install/upgrade <модуль>.\n'
