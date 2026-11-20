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

### Фильтр сетей C24Be

Вкладка **«Сети РКН / VK»** находится в разделе **«Состояние»** Telegram-бота. Модуль считает заблокированные входящие подключения, уникальные IP-адреса источников и исходящие подключения клиентов к сетям VK/Max/OK. Списки обновляются ежедневно с сохранённой копией на VPS для работы при недоступности источника. Первичная загрузка списков требует подключения к GitHub; сами списки не вложены в установочный архив. Фильтрация изначально выключена.

На уже установленном сервере запусти:

```bash
curl -fsSL https://raw.githubusercontent.com/vladdust/amnezia-vps-installer/main/install-network-guard.sh | sudo bash
```

Затем открой бота → **Состояние** → **Сети РКН / VK**, обнови списки и включи нужные направления.

## Отдельный бот для существующей 3x-ui

Для уже установленной панели доступен самостоятельный Telegram-бот. Он работает в отдельной systemd-службе, управляет клиентами через API 3x-ui и не меняет Amnezia Server Kit или саму панель.

```bash
curl -fsSL https://raw.githubusercontent.com/vladdust/amnezia-vps-installer/main/install-3xui-bot.sh | sudo bash
```

Описание и список кнопок: [`3x-ui-bot/README.md`](3x-ui-bot/README.md).
