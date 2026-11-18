# Android VpnService Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Блокировать сайты и применять независимые дневные лимиты на Android, управляя правилами вручную через USB.

**Architecture:** Форк закреплённого Rethink/Firestack предоставляет VpnService и сетевой движок. Модуль Stopscroll хранит конфигурацию и бюджеты, публикует снимок для DNS/flow callbacks и принимает команды от UI и shell-only ContentProvider. Python CLI использует ADB, без фоновой синхронизации.

**Tech Stack:** Kotlin, Android SDK и Gradle wrapper выбранного upstream, Firestack AAR, Android AtomicFile/Binder/VpnService, Python 3.11+ stdlib, ADB.

Статус: реализация установлена на телефон как `30000071`. Модель, журнал,
фильтр, защищённый provider, экран и ручные команды проверены по исходникам,
1 425 Android host-тестам и 276 Python-тестам. Выполнена аппаратная проверка
на Android 13 по Wi-Fi IPv4. Фиксированный RPC: [протокол](../android/PROTOCOL.md).

| Аппаратная проверка | Фактический результат |
| --- | --- |
| Подписанная установка и перенос конфигурации | Политики устройств совпадают; расход телефона не импортируется с desktop |
| Обычный HTTPS, исключение и закрытый YouTube | Проверены из отдельного UID; посторонний UID не получает доступ к provider |
| TCP/UDP и ранний close | Живые потоки закрываются, свежие попытки блокируются; остаток возвращён однократно |
| Повтор RPC | Повтор не резервирует время снова и не возвращает расход повторно |
| Минутный deadline / Doze / Wi-Fi выключен | Native cutoff +17 / +42 / +11 мс; позднее чтение не подменяет момент отключения |
| Запрещённые уведомления | Deadline работает на телефоне |
| Chrome / HTTP/3 | Свежие GET/API проходят в отрезке и не получают HTTP после close и повторной попытки |
| Обновление и перезагрузка | Сохранены политика, timezone и расход; новый boot списывает резерв полностью |
| Экран и USB одновременно | Устаревшее действие получает CONFLICT, новый grant не возникает; UI close работает |
| Отсоединение USB | Пользователь подтвердил: X и YouTube не загружаются без кабеля, обычный сайт работает |
| Desktop и Telegram | Правила и действующая защита сохранены; новые Android-команды установлены отдельно от ключей/Telegram |
| 04:00 / DST | Host-тесты; часы телефона физически не переводились |
| Мобильная сеть / roaming / IPv6 | Вне согласованной текущей аппаратной приёмки, не выдаются за проверенные |

Первоначальные чекбоксы ниже описывают более широкую программу. Фактический объём
и ограничения аппаратной проверки указаны в этой матрице; зелёные host-тесты не
заменяют непроведённые сценарии.

## Global Constraints

- Цель приёмки: Android 13, user 0, arm64-v8a; root и unlock не используются.
- Текущая аппаратная приёмка по уточнению пользователя: только Wi-Fi IPv4; мобильные данные и роуминг не включать, IPv6 не выдавать за проверенный.
- Rethink v0.5.8 / `b750334a363065b94f83e818ac5dc38048713c1f`; Firestack `d3d530ea3e`, до исполнения разрешить в полный SHA и зафиксировать SHA-256 AAR.
- Адресный native-патч допустим только к этому upstream-коммиту; оригинальный и собранный AAR, patch и toolchain имеют отдельные хеши и исходники.
- Пакет `com.celzero.bravedns.stopscroll`; постоянный собственный release signing key вне Git.
- Только ручной USB/ADB: без udev, watcher, timers, фоновой синхронизации, сетевого control server.
- Независимые бюджеты телефона; отрезки, reset 04:00, расход во сне, монотонные deadlines.
- 100 групп, 1 000 domains/exceptions, policy 64 KiB; RPC request/response 96 KiB; последние 64 результата мутаций.
- Provider принимает только Binder UID 2000; Android call() сам не проверяет наши права.
- Перенос конфигурации не переносит расход/сессии и не обнуляет бюджет телефона.
- Изменения по ADB немедленные; delayed changes, bonus и общий бюджет устройств исключены.
- Не расшифровывать HTTPS, не очищать браузерные данные, не обещать блокировку офлайн-кэша или любого неизвестного DoH.
- Ошибка адаптера/журнала не разрешает сайты; saved revision и applied revision различаются.
- На каждом этапе сначала контракты и тесты важных инвариантов, затем код, проверка и отдельный commit. Реальный трафик проверяется на устройстве, не по зелёному unit test.

## Файлы и границы

В основном репозитории:

```text
android/upstream.lock.json              # точные commits, SHA-256 AAR, toolchain и build variant
android/stopscroll-rethink.patch        # все изменения upstream, включая новые Kotlin-файлы
android/build.sh                        # проверка lock, применение патча, release APK
android/install.sh                      # ручная установка проверенного APK через USB
android/README.md                       # сборка, настройка, ограничения, восстановление
android/LEGAL/                         # лицензии и notices включённых компонентов
android/tests/protocol-vectors.json     # общий wire/schema/matching контракт
android/tests/budget-vectors.json       # события времени и ожидаемые бюджеты
android/tests/device_acceptance.py      # ручной запуск сценариев приёмки
android/tests/probe/                    # test-only APK с отдельным UID для сети/IPC
stopscroll/android_policy.py           # codec, normalization, diff и экспорт desktop
stopscroll/android_transport.py        # строго USB, ADB вызов, bounded codec ответа
stopscroll/android_cli.py              # подкоманды android; без зависимости daemon→adb
stopscroll/cli.py                       # ранний dispatch android до desktop exchange
stopscroll/daemon.py                    # owner-only read-only export-sites
tests/test_android_policy.py
tests/test_android_transport.py
tests/test_android_cli.py
tests/test_daemon.py
```

В исключённом из Git `android/rethink-app/`, namespace
`app/src/main/java/com/celzero/bravedns/stopscroll/`:

```text
Policy.kt / PolicyCodec.kt              # строгие типы/валидация и canonical JSON
RpcCodec.kt                            # request/reply, op/args/error codes и bounded JSON
PolicyMatcher.kt                       # домены и исключения по группам
LimitEngine.kt                         # чистые переходы учёта/сессий без Android UI
StateStore.kt                          # AtomicFile, единый журнал, recovery
StopscrollController.kt                # mutex, revision, RPC dedup, snapshots
EngineAdapter.kt                       # граница DNS/flow, reconfigure/close, applied
UsbControlProvider.kt                  # UID, размеры, RPC; не владеет логикой бюджета
StopscrollActivity.kt                  # состояние и открытие/закрытие отрезков
```

Точки интеграции upstream: `service/BraveVPNService.kt`, `service/TunFlowManager.kt`,
`tunnel/TunDnsManager.kt`, `net/go/GoVpnAdapter.kt`, `AndroidManifest.xml`,
`app/build.gradle.kts`. Тесты модуля располагаются в соответствующих
`app/src/test/.../stopscroll/` и `app/src/androidTest/.../stopscroll/`.

## Task 0: G0 — проверить основу до разработки продукта

**Files:** `android/upstream.lock.json`, `android/build.sh`, начальный patch;
upstream build config/manifest и временный test provider в patch.

**Consumes:** закреплённый upstream и целевой телефон. **Produces:** lock с полными
значениями и приватный отчёт G0 с командами/результатами; решение pass/hold.

- [ ] Получить exact commit Rethink; проверить исходные notices и dependency graph.
  В lock записать `upstream_commit`, `firestack_commit`, `firestack_aar_sha256`,
  `gradle_distribution_sha256`, `jdk_major`, `compile_sdk`, `target_sdk`,
  `build_variant`, `resolver`, `dns_alg`. Все значения берутся из проверенной сборки.
- [ ] Проверить доступность опубликованного AAR `com.celzero:firestack:d3d530ea3e`;
  если недоступен — собрать тот же commit и зафиксировать его provenance/хеш,
  не менять source revision ради прохождения Gradle.
- [ ] Добавить build type `stopscrollRelease`: release optimizations,
  `applicationIdSuffix = ".stopscroll"`, debuggable=false, собственная signing config;
  использовать `fdroidFull` без Firebase/Play Billing. Настроить release keep rules
  provider/activity и проверить manifest merger/ссылки package ID.
- [ ] Собрать `./gradlew :app:assembleFdroidFullStopscrollRelease`, проверить
  `apksigner verify --print-certs` и `apkanalyzer manifest print`; ожидаются собственный
  package ID, release-сертификат и `debuggable=false`. Ключ 0600 хранить вне checkout.
- [ ] На тестовом варианте provider вернуть реальный Binder UID на вызов
  `adb -d shell content call --user 0 --uri content://com.celzero.bravedns.stopscroll.usb --method rpc --arg BASE64`.
  Ожидается UID 2000; тестовый APK с обычным UID получает SecurityException.
- [ ] Зафиксировать `https://cloudflare-dns.com/dns-query` и его bootstrap без рекурсии;
  проверить отсутствие молчаливого fallback. Проверить полный DNS+Firewall, A/AAAA, TCP и UDP/QUIC; доменный запрет, Google
  login и разрешённый сайт. Вызвать существующий `GoVpnAdapter.closeConnections(emptyList(), reason=...)`,
  доказать закрытие server-side, не только запись в logcat. Проверить DNS ALG/FakeDNS,
  кэш DNS, заранее открытый YouTube и свежую ленту после закрытия.
- [ ] Создать test-only probe с отличающимся package/UID, выполняющий запросы к
  контролируемому HTTPS/media/UDP fixture. Проверить его UID в firewall. Не измерять
  эффективность запросами самого VPN-приложения. Тестовый CA доверять только probe,
  без изменения системного trust store. Production APK не зависит от probe.
- [ ] Проверить Always-on/lockdown, Doze и ограниченный wake lock, смену Wi-Fi/mobile,
  reboot. Сохранить рабочий resolver/ALG в lock, отметить измеренное время закрытия.
  G0 HOLD при непроходящей сборке, неподтверждённом UID, обходе свежего YouTube или
  невозможности закрыть TCP/UDP. До устранения причины задачи 1–6 не выполнять.
- [ ] Закрыть гонку между разрешением и поздней регистрацией соединения native-патчем:
  admission generation захватывается до policy callback, меняется перед обходом всех
  TCP/UDP handlers и проверяется после Track до копирования данных. Старые trapped DNS
  sockets отсекаются тем же барьером. Проверить оба порядка Track/check/cutoff и in-flight
  DNS на производственных обработчиках. Native-контекст при смене правил сохраняется.
  Пересобрать тот же engine source, сохранить patch/проверки/provenance и повторить приёмку.
  Mobile/IPv6 временно исключены из текущей аппаратной приёмки по уточнению пользователя.
- [ ] Экспортировать минимальный воспроизводимый patch и commit
  `build: pin and validate Android VPN base`; исключить checkout/APK/keys из Git.

## Task 1: Правила, дневной бюджет и crash-safe журнал

**Files:** patch: `Policy*`, `LimitEngine.kt`, `StateStore.kt`; оба JSON vectors;
upstream unit tests `PolicyMatcherTest`, `LimitEngineTest`, `StateStoreTest`.

**Consumes:** policy schema из дизайна. **Produces:** чистый engine:

```kotlin
data class ClockSample(val wallMs: Long, val elapsedMs: Long, val bootCount: Int)
interface LimitEngine {
    fun open(site: String, seconds: Int, clock: ClockSample): Transition
    fun close(site: String, clock: ClockSample): Transition
    fun tick(clock: ClockSample): Transition
    fun replacePolicy(policy: Policy, clock: ClockSample): Transition
    fun snapshot(clock: ClockSample): PolicySnapshot
}
```

`Transition` содержит новое JournalState и список эффектов OPEN/CLOSE/WARN/RECONFIGURE;
чистый engine не выполняет IO. Controller записывает JournalState перед эффектами.

- [ ] Зафиксировать vectors: границы поддоменов, исключение своей группы,
  перекрывающиеся группы, Unicode только до нормализации, лишние поля/лимиты размера.
- [ ] Добавить бюджетный пример: 45 минут, open 10 минут, close через 120 секунд →
  расход 120 секунд, остаток 2 580; второй close не меняет остаток. Повторный open
  активной группы возвращает SESSION_ACTIVE. Проверить исчерпание и уменьшение бюджета.
  До close UI/status показывают 43 минуты остатка, включая 8 минут текущего резерва.
- [ ] Добавить vectors для 03:59→04:00, DST, backward wall clock, process death на том
  же boot, нового boot с полным списанием резерва, удаления/возврата того же ID,
  policy replacement при активных сессиях и коротких отрезков без предупреждения.
  Идентичная policy не закрывает отрезок; 1 001-й ID за день отклоняется, существующий
  расход не вытесняется, смена дня не сбрасывает revision.
- [ ] Реализовать строгие codec/matcher и чистые переходы по vectors. Хранить
  timezone, day_id, spent/reserved и session deadline в одном AtomicFile под mutex.
  Инъецировать сбой записи: open не выдаёт доступ, прошлый журнал читается,
  повреждённые обе копии дают ошибку/закрытие, а не пустую policy.
- [ ] Запустить `./gradlew :app:testFdroidFullStopscrollReleaseUnitTest`; все vectors
  и fault-injection проходят. Экспортировать patch, commit `feat: persist Android site budgets`.

## Task 2: Controller и реальная фильтрация сессий

**Files:** patch: `StopscrollController.kt`, `EngineAdapter.kt`, upstream service,
DNS/flow callbacks; тесты `ControllerTest`, `EngineAdapterTest`.

**Consumes:** LimitEngine/StateStore. **Produces:** `Controller.execute(RpcRequest): RpcReply`,
`Controller.snapshot(): PolicySnapshot`, `EngineAdapter.apply(snapshot): AppliedResult`.
AppliedResult сообщает revision и подтверждённые closure/resolver operations либо ошибку.

- [ ] Добавить тест сериализации open/replace/close: snapshot нельзя открыть до
  fsync резерва; после сохранения запрета новые callbacks его видят даже пока
  старые соединения закрываются. При ошибке адаптера granted snapshot не возвращается.
- [ ] Внедрить gate Stopscroll до upstream trust/allow, перехватить DNS и соединения;
  сохранить независимость исключений. На ужесточениях закрывать все потоки и обновлять
  резолвер, публиковать applied_revision только после подтверждения. Не сбрасывать
  разрешённые соединения каждую секунду.
- [ ] Реализовать serial controller, bounded mutation dedup в общем журнале,
  монотонный tick/deadline, bounded wake lock активных сессий. Параллельная UI-команда
  не обходит expected_revision и не создаёт второй бюджет.
- [ ] Проверить реальные разрешённые сессии: локальный HTTPS/media fixture считает
  запросы и байты. До open не получает запросов, во время open получает, после
  close/deadline поток прекращается ≤2 секунд. Отдельно TCP и UDP/QUIC, сон и Wi-Fi/mobile.
- [ ] Инъецировать exception/timeout apply, остановку движка и восстановление: статус
  degraded, saved/applied различаются, tunnel drop либо закрытие TUN под проверенным
  lockdown не оставляет старый grant. При отсутствии lockdown — честный unprotected.
  Unit suite и fixtures
  проходят; обновить patch, commit `feat: enforce Android sessions in VPN traffic`.

## Task 3: Защищённый протокол ADB

**Files:** patch: `UsbControlProvider.kt`, manifest, `RpcCodec.kt`;
`UsbControlProviderTest` (instrumented), protocol vectors.

**Consumes:** Controller.execute и schema. **Produces:** shell-only `call("rpc", base64)`
с Bundle `result_b64`; неизменяемый контракт пяти op из дизайна.

```kotlin
private fun requireShell() {
    if (android.os.Binder.getCallingUid() != 2000) {
        throw SecurityException("ADB shell required")
    }
}
```

- [ ] Проверить ordinary UID, spoofed caller package, неизвестный method/op, malformed
  base64/JSON, duplicate keys, сверхразмер, wrong revision и неверный request_id.
  Ни один отрицательный случай не меняет журнал и не раскрывает policy постороннему APK.
- [ ] Сделать UID guard первой операцией call(), реализовать bounded decode/strict
  schema; query/insert/update/delete/openFile отклонять, grants не выдавать.
- [ ] Провести mutation через реальный `content call` release APK, сверить ответ,
  policy hash и applied revision. Повторить идентичный request_id → тот же результат;
  другое тело с тем же ID → ошибка; оборвать ADB и проверить отсутствие второго резерва.
- [ ] Прогнать instrumented тесты и ручной UID-тест отдельного APK; экспортировать patch,
  commit `feat: add shell-only Android control provider`.

## Task 4: Desktop экспорт и ручной CLI

**Files:** `stopscroll/android_{policy,transport,cli}.py`, `cli.py`, `daemon.py`,
`tests/test_android_*.py`, `tests/test_daemon.py`.

**Consumes:** стабильный RPC из задачи 3. **Produces:** команды из дизайна;
`AndroidTransport.call(request: dict) -> dict`, `export_sites(state) -> dict`.

- [ ] Тестировать export-sites через существующую peer authentication: чужой UID
  получает отказ; ответ не содержит Telegram, расход, бонусы, pending и owner UID.
  Создать read-only ветку daemon.handle; она не меняет state и не открывает сайты.
- [ ] Реализовать общий нормализатор на основе desktop normalize_domain, строгую
  проверку policy и детерминированный diff. Проверить, что import переносит режимы,
  исключения и бюджеты, но не runtime. Новые Android модули не импортируются daemon.
- [ ] Тестировать transport с fixture stdout `Bundle[{result_b64=...}]`: unknown/multiple
  ключи, плохой JSON, превышение размера, чужой request_id, неавторизованный/несколько
  USB-телефонов, wireless-only, timeout и `adb` отсутствует. Применить subprocess argv
  и корректное remote-shell quoting; не использовать shell=True и автоматический retry мутаций.
- [ ] Добавить ранний dispatch `android` в cli.main до desktop exchange; реализовать
  status/list/export/import/apply/block/unblock/limit/open/close/doctor. Preview и
  подтверждение мутаций обязательны; dry-run не пишет. CONFLICT не повторять автоматически.
  Doctor различает локальные факты и сетевую приёмку из другого UID, не объявляет
  фильтрацию проверенной по одному status или запросу самого VPN-процесса.
- [ ] Проверить полный roundtrip с release APK, одновременно изменить UI и policy по USB;
  получить последовательное изменение либо CONFLICT. Отсоединить кабель и доказать
  продолжение фильтрации. `python3 -m unittest discover -s tests -q` и Kotlin suite проходят.
- [ ] Commit `feat: manage Android rules manually over USB`.

## Task 5: Экран телефона, lifecycle и упаковка

**Files:** patch: `StopscrollActivity.kt`, notification/setup integration;
`android/build.sh`, `android/install.sh`, `android/README.md`, `android/LEGAL/`, `.gitignore`.

**Consumes:** Controller и стабильный APK. **Produces:** повседневный клиент,
подписанная обновляемая сборка, явные install/recovery-команды.

- [ ] Экран показывает active/degraded/stopped, timezone/reset, remaining и session;
  открывает/закрывает через controller. Нет UI для увеличения бюджета, исключения
  приложений или обхода overlay upstream-правилами. При denied VPN status unprotected.
- [ ] Подключить уведомления работы/предупреждения, восстановление после обычного
  убийства процесса, Always-on и перезагрузки с ожидаемой блокировкой до unlock.
  Проверить Android 13 notification permission denied: лимит всё равно закрывается.
- [ ] Установщик проверяет APK package, certificate, version и подключение USB user 0;
  сам не включает root/unlock и не удаляет другие приложения. Повторный install -r
  сохраняет policy, budget, timezone и сертификат. Отсутствие ключа не создаёт новый молча.
- [ ] Описать ручной initial setup VPN/Always-on/lockdown, HyperOS autostart/battery,
  browser Secure DNS, независимые бюджеты, немедленные ADB-ослабления и recovery
  отключением нашего VPN через системные настройки. Не предлагать удаление других приложений.
  Проверять `isIgnoringBatteryOptimizations` и настраивать исключение только нашего
  пакета: без него Doze игнорирует ограниченный partial wake lock. Отдельно подтвердить
  actual elapsedRealtime trigger/retirement, не считать поздний RPC-просып доказательством.
- [ ] Выключить по умолчанию посещенческие query/connection logs и телеметрию;
  не включать дополнительные upstream-блоклисты. Проверить выходные соединения
  собственной сборки: без неожиданного Firebase/analytics или обязательной учётной записи.
- [ ] Проверить APK manifest/permissions, dependency licenses, known-secret scan,
  `.gitignore` для checkout/APK/AAR/keystore/реальных правил/отчётов. Commit
  `feat: package Android session controls and setup`.

## Task 6: Приёмка и интеграция

**Files:** `android/tests/device_acceptance.py`, `android/README.md`, `FOLLOW_UPS.md`;
приватный локальный отчёт устройства вне Git.

- [ ] Запустить ручную матрицу раздела 9 дизайна: свежий сетевой трафик/кэш,
  бюджет/раннее закрытие/04:00, process death/reboot, Doze и смена сети, TCP/UDP/IPv6,
  exceptions и пересечения, USB security, отключение кабеля, APK upgrade.
- [ ] Тестовый профиль/fixture имеет короткие сроки; не расходовать и не сбрасывать
  реальные desktop-бюджеты ради проверки. Проверить службой desktop и Telegram,
  что прежние настройки/права остались прежними.
- [ ] Отчёт содержит фактический APK hash/cert, engine SHA, config hash, status/applied,
  наблюдения сервера, measured closure delay и power/lifecycle результаты. Зеленые
  unit tests не заменяют недоступную проверку; непроверенное явно ограничивает релиз.
- [ ] Провести независимое security/correctness review после реализации, исправить
  все значимые замечания с адресными тестами. Проверить patch с чистого pinned checkout,
  повторить затронутую приёмку, затем интегрировать проверенные исходники и документацию.

## Порядок и критерий готовности

Задачи 0 → 1 → 2 → 3 → 4 → 5 → 6. Сначала доказательство работоспособности основы,
затем бюджет и фильтр, потом USB и UI. Не начинать с оболочки CLI вокруг непроверенного VPN.
Готовность: пользователь без подключённого компьютера открывает отрезок на телефоне,
сервис закрывает сеть по его окончании, расход переживает перезапуски, а ручная USB-команда
читает/меняет действующие правила с подтверждённой applied revision.
