#!/usr/bin/env bash
set -Eeuo pipefail
set +x

# Bootstrapper for: curl -fsSL https://raw.githubusercontent.com/vladdust/amnezia-vps-installer/main/install.sh | sudo bash
REPOSITORY="${VPN_INSTALLER_REPOSITORY:-vladdust/amnezia-vps-installer}"
BRANCH="${VPN_INSTALLER_BRANCH:-main}"
TMP_DIR=""

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
cleanup() { [ -z "$TMP_DIR" ] || python3 - "$TMP_DIR" <<'PY'
from pathlib import Path
import shutil, sys
shutil.rmtree(Path(sys.argv[1]), ignore_errors=True)
PY
}
trap cleanup EXIT

if [ "$(id -u)" -ne 0 ]; then
    die "Запустите команду с sudo: curl -fsSL https://raw.githubusercontent.com/vladdust/amnezia-vps-installer/main/install.sh | sudo bash"
fi
command -v curl >/dev/null 2>&1 || die "Нужен curl"
command -v python3 >/dev/null 2>&1 || die "Нужен Python 3"
[[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "Неверное имя GitHub-репозитория"
[[ "$BRANCH" =~ ^[A-Za-z0-9_./-]+$ ]] || die "Неверное имя ветки"

TMP_DIR=$(mktemp -d /tmp/amnezia-vps-installer.XXXXXX)
chmod 0700 "$TMP_DIR"
ARCHIVE="$TMP_DIR/source.zip"
URL="https://codeload.github.com/$REPOSITORY/zip/refs/heads/$BRANCH"
printf 'Загружаю локальный пакет из %s (%s)…\n' "$REPOSITORY" "$BRANCH"
curl --fail --silent --show-error --location --retry 3 --proto '=https' --tlsv1.2 "$URL" -o "$ARCHIVE" ||
    die "Не удалось загрузить публичный GitHub-репозиторий. Проверьте имя и доступность сети."

python3 - "$ARCHIVE" "$TMP_DIR/source" <<'PY'
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import zipfile

archive, destination = Path(sys.argv[1]), Path(sys.argv[2])
destination.mkdir(mode=0o700)
with zipfile.ZipFile(archive) as zipped:
    entries = zipped.infolist()
    roots = set()
    for item in entries:
        name = item.filename
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise SystemExit("В архиве обнаружен небезопасный путь")
        roots.add(path.parts[0])
        mode = item.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise SystemExit("Архив содержит символическую ссылку")
    if len(roots) != 1:
        raise SystemExit("Неожиданная структура GitHub-архива")
    for item in entries:
        path = PurePosixPath(item.filename)
        relative = Path(*path.parts[1:])
        if not relative.parts:
            continue
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True, mode=0o755)
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        with zipped.open(item) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
        mode = item.external_attr >> 16
        target.chmod(0o755 if mode & 0o111 else 0o644)

installer = destination / "install-local.sh"
PY

PACKAGE_DIR="$TMP_DIR/source"
if [ ! -f "$PACKAGE_DIR/install-local.sh" ] && [ -f "$TMP_DIR/source/installer-package.zip" ]; then
    PACKAGE_DIR="$TMP_DIR/package"
    mkdir -m 0700 "$PACKAGE_DIR"
    python3 - "$TMP_DIR/source/installer-package.zip" "$PACKAGE_DIR" <<'PY'
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import zipfile

archive, destination = Path(sys.argv[1]), Path(sys.argv[2])
expanded = 0
with zipfile.ZipFile(archive) as zipped:
    for item in zipped.infolist():
        path = PurePosixPath(item.filename)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise SystemExit("Во вложенном пакете найден небезопасный путь")
        if path.parts[0] == "vendor":
            raise SystemExit("Пакет не должен содержать распакованный каталог vendor")
        if stat.S_ISLNK(item.external_attr >> 16):
            raise SystemExit("Пакет содержит символическую ссылку")
        expanded += item.file_size
        if expanded > 50 * 1024 * 1024:
            raise SystemExit("Распакованный пакет превышает 50 МБ")
        target = destination.joinpath(*path.parts)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True, mode=0o755)
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        with zipped.open(item) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
        target.chmod(0o755 if item.external_attr >> 16 & 0o111 else 0o644)
PY
fi

[ -f "$PACKAGE_DIR/install-local.sh" ] || die "В GitHub-репозитории не найден install-local.sh или installer-package.zip"
if [ ! -f "$PACKAGE_DIR/vendor/amnezia-server-kit-main/install-vpn-kit.sh" ] ||
   [ ! -f "$PACKAGE_DIR/vendor/tspublock-master/update_cyberok.sh" ]; then
    [ -f "$PACKAGE_DIR/prepare-vendor.py" ] || die "В репозитории нет подготовщика локальных снимков исходников"
    printf 'Распаковываю проверенные снимки исходников из локального пакета…\n'
    python3 "$PACKAGE_DIR/prepare-vendor.py" "$PACKAGE_DIR" || die "Не удалось проверить и подготовить исходники проектов"
fi

printf 'Архив распакован; запускаю установщик.\n'
bash "$PACKAGE_DIR/install-local.sh"
