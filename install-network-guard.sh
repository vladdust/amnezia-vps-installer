#!/usr/bin/env bash
set -Eeuo pipefail
set +x

BOT="/opt/vpn-bot/bot.py"
CONFIG="/etc/vpn-bot/config.json"
SERVICE="vpn-bot.service"
MANAGER="/usr/local/sbin/vpn-bot-module"
PACKAGE_URL="https://raw.githubusercontent.com/vladdust/amnezia-vps-installer/main/installer-package.zip"
TMP_DIR=""
BACKUP=""
CATALOG="/opt/vpn-bot/module-catalog/as-network-guard"
CATALOG_BACKUP=""

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
cleanup() { [ -z "$TMP_DIR" ] || rm -rf -- "$TMP_DIR"; }
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || die "Запусти команду от root через sudo."
for cmd in curl python3 systemctl install stat; do
    command -v "$cmd" >/dev/null 2>&1 || die "Не найдена команда $cmd"
done
[ -x "$MANAGER" ] || die "Сначала установи менеджер модулей: enable-modules.sh"
[ -f "$BOT" ] && [ ! -L "$BOT" ] || die "Не найден обычный файл бота $BOT."
[ -f "$CONFIG" ] || die "Не найден $CONFIG."
systemctl cat "$SERVICE" >/dev/null 2>&1 || die "Не найдена служба $SERVICE."
systemctl is-active --quiet "$SERVICE" || die "Служба $SERVICE не запущена. Сначала проверь её состояние."
[ ! -L /opt/vpn-bot/module-catalog ] || die "Каталог модулей является символической ссылкой."
[ ! -L "$CATALOG" ] || die "Каталог сетевого модуля является символической ссылкой."

TMP_DIR=$(mktemp -d /tmp/vpn-bot-network-guard.XXXXXX)
chmod 0700 "$TMP_DIR"
ARCHIVE="$TMP_DIR/installer-package.zip"
SOURCE="$TMP_DIR/source"
mkdir -m 0700 "$SOURCE"

printf 'Загружаю файлы модуля и обновление меню…\n'
curl --fail --silent --show-error --location --retry 3 --proto '=https' --tlsv1.2 "$PACKAGE_URL" -o "$ARCHIVE" ||
    die "Не удалось загрузить пакет. Сервер не изменён."

python3 - "$ARCHIVE" "$SOURCE" <<'PY'
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import zipfile

archive, destination = Path(sys.argv[1]), Path(sys.argv[2])
allowed = {
    "modules/patch-bot-loader.py",
    "modules/catalog/as-network-guard/manifest.json",
    "modules/catalog/as-network-guard/bot_module.py",
    "modules/catalog/as-network-guard/network_guard.py",
    "modules/catalog/as-network-guard/as-network-guard.service",
    "modules/catalog/as-network-guard/as-network-guard-refresh.service",
    "modules/catalog/as-network-guard/as-network-guard-refresh.timer",
    "modules/catalog/as-network-guard/as-network-guard-snapshot.service",
    "modules/catalog/as-network-guard/as-network-guard-snapshot.timer",
}
found, expanded = set(), 0
with zipfile.ZipFile(archive) as zipped:
    for item in zipped.infolist():
        path = PurePosixPath(item.filename)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise SystemExit("В архиве обнаружен небезопасный путь")
        if stat.S_ISLNK(item.external_attr >> 16):
            raise SystemExit("Архив содержит символическую ссылку")
        name = path.as_posix()
        if name not in allowed or item.is_dir():
            continue
        if name in found:
            raise SystemExit("В пакете повторяется файл " + name)
        expanded += item.file_size
        if expanded > 5 * 1024 * 1024:
            raise SystemExit("Файлы модуля превышают допустимый размер")
        target = destination.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipped.open(item) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
        found.add(name)
missing = sorted(allowed - found)
if missing:
    raise SystemExit("В пакете не найдены файлы: " + ", ".join(missing))
PY

PATCHER="$SOURCE/modules/patch-bot-loader.py"
MODULE="$SOURCE/modules/catalog/as-network-guard"
python3 -m py_compile "$PATCHER" "$MODULE/bot_module.py" "$MODULE/network_guard.py" || die "Проверка Python-файлов не пройдена. Сервер не изменён."

cp -p "$BOT" "$TMP_DIR/bot.preflight.py"
python3 "$PATCHER" "$TMP_DIR/bot.preflight.py" || die "Эта версия bot.py не совпала с ожидаемыми точками расширения. Сервер не изменён."
python3 -m py_compile "$TMP_DIR/bot.preflight.py" || die "Изменённое меню не прошло синтаксическую проверку. Сервер не изменён."

STAMP=$(date -u '+%Y%m%dT%H%M%SZ')
BOT_MODE=$(stat -c '%a' "$BOT")
BOT_UID=$(stat -c '%u' "$BOT")
BOT_GID=$(stat -c '%g' "$BOT")
BACKUP_DIR="/var/backups/vpn-bot-network-guard/$STAMP"
install -d -o root -g root -m 0700 "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/bot.py"
cp -p "$BOT" "$BACKUP"
chmod 0600 "$BACKUP"
if [ -d "$CATALOG" ]; then
    CATALOG_BACKUP="$BACKUP_DIR/catalog"
    cp -a "$CATALOG" "$CATALOG_BACKUP"
fi

install -d -o root -g root -m 0750 "$CATALOG"
cp -a "$MODULE/." "$CATALOG/"
chown -R root:root "$CATALOG"
find "$CATALOG" -type d -exec chmod 0750 {} +
find "$CATALOG" -type f -exec chmod 0640 {} +

if ! python3 "$PATCHER" "$BOT"; then
    install -o "$BOT_UID" -g "$BOT_GID" -m "$BOT_MODE" "$BACKUP" "$BOT"
    die "Не удалось обновить вкладку состояния. bot.py восстановлен из $BACKUP"
fi

if python3 - <<'PY'
import json
from pathlib import Path
try:
    registry = json.loads(Path('/var/lib/vpn-bot/module-manager/registry.json').read_text(encoding='utf-8'))
    raise SystemExit(0 if registry.get('modules', {}).get('as-network-guard', {}).get('installed') else 1)
except (OSError, ValueError):
    raise SystemExit(1)
PY
then
    OPERATION=upgrade
else
    OPERATION=install
fi

if ! "$MANAGER" "$OPERATION" as-network-guard; then
    install -o "$BOT_UID" -g "$BOT_GID" -m "$BOT_MODE" "$BACKUP" "$BOT"
    if [ -n "$CATALOG_BACKUP" ]; then
        rm -rf -- "$CATALOG"
        cp -a "$CATALOG_BACKUP" "$CATALOG"
    else
        rm -rf -- "$CATALOG"
    fi
    systemctl restart "$SERVICE" || true
    die "Модуль не установился. bot.py восстановлен; резервная копия: $BACKUP"
fi

printf '\nГотово. Вкладка «Сети РКН / VK» добавлена в раздел «Состояние».\n'
printf 'Фильтрация по умолчанию выключена и не меняет firewall до включения кнопкой в боте.\n'
printf 'Резервная копия bot.py: %s\n' "$BACKUP"
