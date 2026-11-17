# Установщик Amnezia Server Kit для AmneziaWG

Установщик для VPS с уже установленным AmneziaWG через AmneziaVPN. Включает Amnezia Server Kit, интеграцию CyberOK и менеджер модулей Telegram-бота.

```bash
curl -fsSL https://raw.githubusercontent.com/vladdust/amnezia-vps-installer/main/install.sh | sudo bash
```

Пакет включает ZIP-снимки `s1riller/amnezia-server-kit` и `upe4d/tspublock`, использованные для этой сборки. Установщик распаковывает их локально и сверяет контрольные суммы из `SHA256SUMS`. Снимки не обновляются автоматически. Авторство исходных проектов указано в `THIRD_PARTY_NOTICES.md`.

## Требования

- Ubuntu 22.04+ или Debian 12+;
- AmneziaWG от AmneziaVPN уже установлен и запущен;
- Docker-контейнер `amnezia-awg*` и сеть `amnezia-dns-net`;
- доступ к GitHub, пакетному серверу ОС, Telegram и источникам CyberOK.

Установщик не добавляет другие VPN-протоколы. Он запускает Amnezia Server Kit с AdGuard Home, защитой сервера и Telegram-ботом. Список CyberOK применяется к ipset. GOVIPS, сбор RST и веб-сервер `tspublock` не устанавливаются.

Токен бота вводится скрыто и передаётся дочернему процессу через переменную окружения. Первый администратор регистрируется командой `/start КОД`, которую установщик показывает после запуска.

## Если Server Kit уже установлен

Менеджер модулей можно установить отдельно, без повторной установки Server Kit:

```bash
curl -fsSL https://raw.githubusercontent.com/vladdust/amnezia-vps-installer/main/enable-modules.sh | sudo bash
```

Скрипт проверяет совместимость `bot.py` и сохраняет резервную копию. Он не меняет firewall или AmneziaWG.

## Управление модулями

```text
vpn-bot-module refresh
vpn-bot-module list
vpn-bot-module install <имя-модуля>
vpn-bot-module upgrade <имя-модуля>
vpn-bot-module remove <имя-модуля>
vpn-bot-module rollback <имя-модуля>
```

Модули могут добавлять возможности Telegram-бота, серверные файлы и systemd-службы. Для новых модулей используется каталог `modules/catalog` этого репозитория.

## Бот для существующей 3x-ui

Можно установить отдельного Telegram-бота для уже работающей панели 3x-ui. Он использует API панели и не меняет Amnezia-бота или установку 3x-ui.

```bash
curl -fsSL https://raw.githubusercontent.com/vladdust/amnezia-vps-installer/main/install-3xui-bot.sh | sudo bash
```

Установщик запросит URL панели, API-токен, токен Telegram-бота и ID администратора. Подробности: [`3x-ui-bot.md`](3x-ui-bot.md).
