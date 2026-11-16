#!/usr/bin/env bash
set -Eeuo pipefail
set +x

BOT="/opt/vpn-bot/bot.py"
CONFIG="/etc/vpn-bot/config.json"
SERVICE="vpn-bot.service"
MANAGER="/usr/local/sbin/vpn-bot-module"
PACKAGE_URL="https://raw.githubusercontent.com/vladdust/amnezia-vps-installer/main/installer-package.zip"
TMP_DIR=""

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
cleanup() { [ -z "$TMP_DIR" ] || rm -rf -- "$TMP_DIR"; }
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || die "Запусти команду от root через sudo."
for cmd in curl python3 systemctl install stat; do
    command -v "$cmd" >/dev/null 2>&1 || die "Не найден $cmd"
done
[ -f "$BOT" ] || die "Не найден $BOT. Сначала установи Amnezia Server Kit."
[ ! -L "$BOT" ] || die "$BOT является символической ссылкой; остановка без изменений."
[ -f "$CONFIG" ] || die "Не найден $CONFIG; остановка без изменений."
systemctl cat "$SERVICE" >/dev/null 2>&1 || die "Не найдена служба $SERVICE."
systemctl is-active --quiet "$SERVICE" || die "Служба $SERVICE не запущена; сначала проверь её состояние."
[ -x "$MANAGER" ] || die "Менеджер модулей не установлен. Сначала запусти enable-modules.sh."
grep -q '^# BEGIN BOT MODULE LOADER$' "$BOT" || die "Загрузчик модулей в bot.py не найден. Сначала запусти enable-modules.sh."
[ ! -L /opt/vpn-bot/module-catalog ] || die "Каталог модулей является символической ссылкой."
[ ! -L /opt/vpn-bot/module-catalog/client-lifecycle ] || die "Каталог client-lifecycle является символической ссылкой."

TMP_DIR=$(mktemp -d /tmp/vpn-bot-client-menu.XXXXXX)
chmod 0700 "$TMP_DIR"
ARCHIVE="$TMP_DIR/installer-package.zip"
SOURCE="$TMP_DIR/source"
mkdir -m 0700 "$SOURCE"

printf 'Загружаю файлы обновления…\n'
curl --fail --silent --show-error --location --retry 3 --proto '=https' --tlsv1.2 "$PACKAGE_URL" -o "$ARCHIVE" ||
    die "Не удалось получить пакет. Сервер не изменён."

python3 - "$ARCHIVE" "$SOURCE" <<'PY'
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import zipfile

archive, destination = Path(sys.argv[1]), Path(sys.argv[2])
allowed = {
    "modules/patch-bot-loader.py",
    "modules/catalog/client-lifecycle/manifest.json",
    "modules/catalog/client-lifecycle/bot_module.py",
}
if archive.stat().st_size > 5 * 1024 * 1024:
    raise SystemExit("Архив установщика превышает 5 МБ")
found, expanded = set(), 0
with zipfile.ZipFile(archive) as zipped:
    for item in zipped.infolist():
        path = PurePosixPath(item.filename)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise SystemExit("В пакете обнаружен небезопасный путь")
        mode = item.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise SystemExit("Пакет содержит символическую ссылку")
        name = path.as_posix()
        if name not in allowed or item.is_dir():
            continue
        if name in found:
            raise SystemExit("В пакете повторяется файл " + name)
        expanded += item.file_size
        if expanded > 5 * 1024 * 1024:
            raise SystemExit("Размер выбранных файлов превышает 5 МБ")
        target = destination.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipped.open(item) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
        found.add(name)
missing = sorted(allowed - found)
if missing:
    raise SystemExit("В пакете не найдены необходимые файлы: " + ", ".join(missing))
PY

PATCHER="$SOURCE/modules/patch-bot-loader.py"
CATALOG="$SOURCE/modules/catalog/client-lifecycle"
python3 - "$PATCHER" "$CATALOG/bot_module.py" <<'PY'
from pathlib import Path
import sys
for item in sys.argv[1:]:
    path = Path(item)
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY

# Verify that this exact bot can be updated before writing to the live file.
cp -p "$BOT" "$TMP_DIR/bot.preflight.py"
python3 "$PATCHER" "$TMP_DIR/bot.preflight.py" || die "Не удалось предварительно встроить пункты в меню клиентов. Сервер не изменён."
python3 -m py_compile "$TMP_DIR/bot.preflight.py" || die "Проверка синтаксиса не пройдена. Сервер не изменён."

install -d -o root -g root -m 0750 /opt/vpn-bot/module-catalog/client-lifecycle
install -o root -g root -m 0640 "$CATALOG/manifest.json" /opt/vpn-bot/module-catalog/client-lifecycle/manifest.json
install -o root -g root -m 0640 "$CATALOG/bot_module.py" /opt/vpn-bot/module-catalog/client-lifecycle/bot_module.py

if python3 - <<'PY'
import json
from pathlib import Path
try:
    data=json.loads(Path('/var/lib/vpn-bot/module-manager/registry.json').read_text())
    raise SystemExit(0 if data.get('modules',{}).get('client-lifecycle',{}).get('installed') else 1)
except (OSError, ValueError):
    raise SystemExit(1)
PY
then
    "$MANAGER" upgrade client-lifecycle || die "Не удалось обновить модуль client-lifecycle."
else
    "$MANAGER" install client-lifecycle || die "Не удалось установить модуль client-lifecycle."
fi

STAMP=$(date -u '+%Y%m%dT%H%M%SZ')
BOT_MODE=$(stat -c '%a' "$BOT")
BOT_UID=$(stat -c '%u' "$BOT")
BOT_GID=$(stat -c '%g' "$BOT")
BACKUP_DIR="/var/backups/vpn-bot-client-menu/$STAMP"
install -d -o root -g root -m 0700 "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/bot.py"
cp -p "$BOT" "$BACKUP"
chmod 0600 "$BACKUP"

if ! python3 "$PATCHER" "$BOT"; then
    install -o "$BOT_UID" -g "$BOT_GID" -m "$BOT_MODE" "$BACKUP" "$BOT"
    die "Не удалось изменить меню; bot.py восстановлен из $BACKUP."
fi
if ! systemctl restart "$SERVICE"; then
    install -o "$BOT_UID" -g "$BOT_GID" -m "$BOT_MODE" "$BACKUP" "$BOT"
    systemctl restart "$SERVICE" || true
    die "Бот не перезапустился; bot.py восстановлен из $BACKUP."
fi

printf '\nГотово. «Доступы» и «Неактивные» теперь находятся в меню «Клиенты».\n'
printf 'Резервная копия bot.py: %s\n' "$BACKUP"
printf 'Проверка: sudo vpn-bot-module list\n'
