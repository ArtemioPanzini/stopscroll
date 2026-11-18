# Stopscroll

Stopscroll помогает сдерживать привычку автоматически открывать отвлекающие сайты и выбранные Telegram-каналы. Это исходники для Linux с systemd: Python-служба, командная строка и отдельный патч Telegram Desktop. Готовый клиент и бинарные релизы в репозитории не поставляются.

По умолчанию Twitch, Liquipedia, Kick и Instagram закрыты. YouTube получает **45 минут в день**, X — **10 минут**. Открытие расходует бюджет в пределах отдельного отрезка: по умолчанию 15 минут, с предупреждением за 2 минуты до конца. Новый день начинается в 04:00 местного времени. Ужесточение действует сразу, ослабление, включая разблокировку и удаление, ждёт **45 минут**. Состояние и очередь сохраняются между запусками.

## Установка для сайтов

Нужны Linux, systemd, systemd-resolved, Python 3.11+, dnsmasq (`/usr/sbin/dnsmasq`), nftables, iproute2 (`ss`), `dig` (пакет `dnsutils` в Debian/Ubuntu или `bind-utils` в Fedora/RHEL), sudo и утилиты управления пользователями. Python-код использует стандартную библиотеку. Для уведомлений нужны `notify-send` и графический сеанс владельца. Поддержка macOS, Windows, систем без systemd и пакетных установщиков не предусмотрена.

Получите исходники и перейдите в каталог проекта. Установщик запускают через sudo из обычной учётной записи будущего владельца:

```bash
git clone https://github.com/ArtemioPanzini/stopscroll.git
cd stopscroll
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
sudo bash ./install.sh
stopscroll doctor
```

Установка меняет системную конфигурацию: запускает DNS на `127.0.0.1:5354`, настраивает systemd-resolved, создаёт таблицу nftables `inet stopscroll` с hook `output` и отключает DNS-over-HTTPS политиками Firefox, Chrome и Chromium. Другие ключи политики Firefox сохраняются. `/etc/resolv.conf`, ufw и Docker не изменяются; политики Opera и Brave не устанавливаются.

Код устанавливается в `/usr/local/lib/stopscroll/`, состояние — в `/var/lib/stopscroll/state.json` (root, 0600; каталог 0700). Ручного файла настроек нет: правила меняются командами. Установщик отказывает при постороннем процессе на порту 5354. Повторная установка сохраняет состояние, временно останавливая управляющую службу и сторож, чтобы избежать двух писателей. Успех определяется кодом `doctor`, запущенного от имени владельца.

Если уведомитель не включился, войдите в графический сеанс и выполните:

```bash
systemctl --user daemon-reload
systemctl --user enable --now stopscroll-notify.service
```

## Управление

Команды выполняются владельцем без sudo. Минуты — целые числа от 1 до 1440. Немедленные изменения требуют подтверждения; `--yes` подходит для скриптов.

```bash
stopscroll status
stopscroll doctor
stopscroll open youtube 10
stopscroll open x                  # не больше остатка бюджета X
stopscroll close youtube

stopscroll block youtube
stopscroll block news example.org
stopscroll block news other.org
stopscroll limit youtube 30
stopscroll delay 60
stopscroll remove-exception youtube accounts.youtube.com

stopscroll request budget youtube 60
stopscroll request bonus youtube 10
stopscroll request limit twitch 15
stopscroll request unblock news other.org
stopscroll request unblock news
stopscroll request except youtube example.org
stopscroll request delay 30
stopscroll request pause 20
stopscroll cancel 3
```

`open` работает для сайтов с лимитом и ограничен остатком бюджета. При раннем `close` учитывается использованное время; бюджет расходуется, пока отрезок открыт, независимо от активности вкладки. Увеличение бюджета, сокращение задержки, исключения и паузы проходят через очередь. `status` показывает время применения и номер для отмены. Пауза отсчитывается с момента применения запроса.

Systemd перезапускает упавшие службы; сторож раз в минуту поднимает остановленные. Управляющая служба сверяет настройки раз в минуту. При отказе DNS разрешение имён может временно пропасть; при отказе управляющей службы остаются прежние правила. При повреждённом состоянии служба начинает с блокировки. Для диагностики:

```bash
stopscroll doctor
sudo journalctl -u stopscrolld.service -u stopscroll-dns.service
```

## Android

Отдельное Android-приложение использует локальный VPN для блокировки сайтов и
независимых дневных лимитов. Компьютер нужен для ручного управления правилами по
USB; после отключения кабеля фильтр работает самостоятельно. [Сборка, установка,
проверки и ограничения](android/README.md).

Блокировка отдельных Telegram-каналов на Android пока не реализована;
дальнейшие задачи перечислены в [FOLLOW_UPS.md](FOLLOW_UPS.md).

## Выбранные Telegram-каналы

Нужен отдельно собранный изменённый Telegram Desktop: [сборка, проверка и установка](telegram/README.md). Официальный клиент сам по себе не читает правила Stopscroll.

```bash
stopscroll telegram block @example_channel --yes
stopscroll telegram block https://t.me/another_example/123 --yes
stopscroll request telegram-unblock example_channel
stopscroll status
```

Имена сравниваются точно, без учёта регистра. Поддерживаются публичные ссылки `t.me`, `telegram.me` и `/s/`; приватные приглашения не подходят. Демон публикует `/etc/stopscroll/telegram.json` (0644). Клиент закрывает выбранные каналы в истории, профиле, ссылках, поиске, предпросмотре, историях и медиагалерее. Распознанный ID закрепляется за запретом, чтобы сохранить его после переименования. Снятие запрета ждёт ту же задержку; пауза сайтов не снимает запреты каналов.

[Пример списка](telegram/blocked-channels.example.txt) содержит только вымышленные имена. Этот файл не загружается автоматически. Собственный список держите локально, добавляя нужные имена через CLI; реальные списки исключены из git. Политика содержит имена и ID каналов и доступна локальным пользователям на чтение.

## Ограничения

- Это средство самоконтроля: администратор с sudo может вручную снять ограничения.
- VPN, прокси, Tor, контейнеры и неизвестные реализации DNS-over-HTTPS могут обходить фильтр. Прямой `resolvectl --interface=<интерфейс> query <домен>` также обходит глобальный DNS-фильтр: доступ systemd-resolved к DNS сохранён ради локальных имён.
- Бюджет привязан к открытому отрезку, а не к просмотру. Перевод часов вперёд может дать новый дневной бюджет; задержка ослаблений отслеживается отдельно.
- YouTube Music и встроенные видео входят в YouTube; вне отрезка блокируются также API и программы владельца. `accounts.youtube.com` оставлен для входа в Google; `t.co`, `twimg.com` и `ytimg.com` не блокируются.
- Закрытие соединений использует карантин IP на 120 секунд, что может временно затронуть другие сервисы Google. Буфер видео и DNS-кэш могут задержать видимый результат. Изменения списка обычно доходят до DNS в пределах 5 секунд; отдельная сверка может ждать до минуты.
- Telegram Web, другие клиенты и устройства не ограничиваются. Пересланный текст в разрешённых чатах остаётся видимым. Пока ID канала ещё не распознан, запрет по старому имени не найдёт уже переименованный канал.
- Автоматические тесты проверяют логику и упаковку; они не заменяют проверку DNS, браузеров и входа в Telegram на целевой системе.

## Проверка исходников

Из корня, без root, Docker, сети и учётных данных:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 telegram/tests/build_helper_test.py -q
bash -n install.sh
bash -n uninstall.sh
bash -n telegram/install.sh
```

Основной Python-набор включает проверки сайтов, Telegram, браузерной защиты и Android CLI; отдельный набор helper проверяет сборку Telegram Desktop. Нативные проверки требуют закреплённого checkout Telegram с применённым патчем; команды приведены в [telegram/README.md](telegram/README.md). Полная сборка занимает часы и требует значительного объёма диска и памяти.

## Удаление

Штатный запрос проходит через очередь ослаблений:

```bash
stopscroll request uninstall
```

Удаление снимает службы, правила и установленный клиент Stopscroll, удаляет состояние, сохраняет другие ключи Firefox и пробует восстановить настройки рабочего стола. Данные аккаунта Telegram не удаляются. Обычный установленный Telegram остаётся доступен; перед его запуском полностью завершите Telegram (Stopscroll).

## Аварийное восстановление

Если из-за сбоя пропало разрешение имён, выполните:

```bash
sudo systemctl disable --now stopscroll-watchdog.timer stopscrolld.service stopscroll-dns.service
sudo rm /etc/systemd/resolved.conf.d/stopscroll.conf && sudo systemctl restart systemd-resolved
sudo nft delete table inet stopscroll
```

Ошибки об отсутствующем файле или таблице означают, что соответствующая часть уже снята. Для полного аварийного удаления:

```bash
sudo bash /usr/local/lib/stopscroll/uninstall.sh
```

Сценарий пытается выполнить все шаги даже при отдельных ошибках и возвращает ненулевой код при проблемах. Если восстановление ярлыков Telegram не удалось, сохранённый приватный сценарий можно повторно запустить в графическом сеансе:

```bash
python3 "$HOME/.config/stopscroll/desktop-recovery.py" restore
```

## Лицензия

Исходники Stopscroll распространяются под GNU GPL версии 3: [LICENSE](LICENSE). Патч содержит изменения Telegram Desktop; его авторские уведомления, разрешение на связывание с OpenSSL и источники сохранены в [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), [telegram/LEGAL](telegram/LEGAL) и [telegram/LICENSE](telegram/LICENSE). Названия Telegram и ссылки на upstream служат для указания происхождения проекта.
