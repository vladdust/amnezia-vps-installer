#!/usr/bin/env bash
set -Eeuo pipefail
set +x

REPOSITORY="vladdust/amnezia-vps-installer"
BRANCH="main"
TMP_DIR=""

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
cleanup() { [ -z "$TMP_DIR" ] || python3 - "$TMP_DIR" <<'PY'
from pathlib import Path
import shutil, sys
shutil.rmtree(Path(sys.argv[1]), ignore_errors=True)
PY
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || die "Запустите эту команду с sudo."
command -v curl >/dev/null 2>&1 || die "Нужен curl."
command -v python3 >/dev/null 2>&1 || die "Нужен Python 3."

TMP_DIR=$(mktemp -d /tmp/3x-ui-telegram-bot.XXXXXX)
chmod 0700 "$TMP_DIR"
ARCHIVE="$TMP_DIR/installer-package.zip"
SOURCE_DIR="$TMP_DIR/source"
URL="https://raw.githubusercontent.com/$REPOSITORY/$BRANCH/installer-package.zip"

printf 'Загружаю пакет бота для 3x-ui…\n'
curl --fail --silent --show-error --location --retry 3 --proto '=https' --tlsv1.2 "$URL" -o "$ARCHIVE" ||
    die "Не удалось загрузить пакет с GitHub."

python3 - "$ARCHIVE" "$SOURCE_DIR" <<'PY'
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import zipfile

archive, destination = Path(sys.argv[1]), Path(sys.argv[2])
destination.mkdir(mode=0o700)
expanded = 0
found = set()
with zipfile.ZipFile(archive) as zipped:
    for item in zipped.infolist():
        path = PurePosixPath(item.filename)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise SystemExit("В архиве найден небезопасный путь")
        if path.parts[0] != "3x-ui-bot":
            continue
        if stat.S_ISLNK(item.external_attr >> 16):
            raise SystemExit("Архив содержит символическую ссылку")
        expanded += item.file_size
        if expanded > 8 * 1024 * 1024:
            raise SystemExit("Пакет бота превышает допустимый размер 8 МБ")
        relative = Path(*path.parts[1:])
        if not relative.parts:
            continue
        found.add(relative.as_posix())
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True, mode=0o755)
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        with zipped.open(item) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
        target.chmod(0o755 if item.external_attr >> 16 & 0o111 else 0o644)

required = {"bot.py", "install.sh", "README.md"}
if not required.issubset(found):
    raise SystemExit("В архиве не найдены все файлы бота для 3x-ui")
PY

bash "$SOURCE_DIR/install.sh" "$SOURCE_DIR"
