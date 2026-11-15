# stopscroll — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Цель:** системная блокировка отвлекающих сайтов на этой машине: четыре сайта закрыты насовсем, YouTube и x.com открываются командой на отрезок в пределах дневного бюджета, любое ослабление правил применяется с задержкой.

**Архитектура:** чистое ядро (`Engine`) считает бюджеты, отрезки и очередь изменений и ничего не знает о системе. Системный слой (`System`) приводит машину к состоянию, которое ядро считает должным: конфигурация отдельного `dnsmasq`, таблица `nftables`, настройка `systemd-resolved`, политики браузеров. Служба `stopscrolld` (root) связывает их и слушает unix-сокет; команда `stopscroll` и уведомитель — клиенты сокета.

**Стек:** Python 3.12, только стандартная библиотека; `unittest`; `dnsmasq` 2.90 (`dnsmasq-base`), `nftables`, `systemd`, `systemd-resolved`.

**Спецификация:** `docs/superpowers/specs/2026-11-15-stopscroll-design.md`. Исполнитель читает её вместе с планом; номера разделов ниже — оттуда.

Все пути в плане — от корня репозитория `/home/lfg/projects/stopscroll`. Тесты запускаются из корня: `python3 -m unittest discover -s tests -v`.

## Глобальные ограничения

- Только стандартная библиотека Python 3; сторонних пакетов нет ни в коде, ни в тестах.
- Тесты не требуют root и сети (раздел 8).
- `/etc/resolv.conf`, таблицы `ufw` и Docker не изменяются. Своя таблица — `inet stopscroll`, hook только `output`.
- Изменение списка блокировки действует на DNS не позже чем через 5 секунд; по явному выбору владельца завершение паузы и изменения без события подхватываются минутной сверкой задачи 6.
- Карантин адресов при закрытии отрезка — 120 секунд.
- Значения по умолчанию: задержка 45 мин, смена дня в 04:00 местного времени, отрезок 15 мин, предупреждение за 2 мин до конца отрезка.
- Ручного файла настроек нет. Состояние — один файл `/var/lib/stopscroll/state.json`, владелец root, права 0600, пишет только служба.
- Штатной мгновенной команды «выключить» нет. Ослабление всегда идёт через очередь, как бы его ни вызвали.
- Файл политик Firefox дополняется, а не затирается. Для Opera и Brave политики не ставятся.
- Все сообщения владельцу — на русском. Имена команд, файлов и идентификаторы — на английском.
- Постоянные адреса: порт DNS `127.0.0.1:5354`; системный пользователь `stopscroll-dns`; сокет `/run/stopscroll/stopscroll.sock`; код в `/usr/local/lib/stopscroll/`.

## На что смотреть при ревью

Пять условий, которые спецификация подразумевает, но прямо не описывает. Каждое закреплено тестом в задаче-владельце.

1. **Файл состояния повреждён или пропал** (обрыв питания при записи). Ожидание: служба поднимается с блокировкой, а не открытой. → Задачи 1 и 6.
2. **Системные часы ушли назад** (поправка NTP) при открытом отрезке или около смены дня. Ожидание: нет свежего бюджета, нет отрицательного времени, нет падения. → Задача 2.
3. **Кривой ввод**: `https://www.Twitch.tv/dir`, домен из одной метки (`com`), нулевые, отрицательные и нечисловые минуты, битый JSON в сокете. Ожидание: понятный отказ или нормализация, служба жива. → Задачи 3 и 6.
4. **Системная команда не сработала** (`nft` или `systemctl` вернули ошибку, файл не пишется). Ожидание: цикл службы не падает, сбой записан, следующая сверка пробует снова. → Задача 5.
5. **Изменение в очереди устарело** (сайт удалён или переведён, пока оно ждало). Ожидание: изменение снимается с понятным сообщением, остальные применяются. → Задача 3.

## Раскладка файлов

| Файл | За что отвечает |
|---|---|
| `stopscroll/model.py` | Классы состояния и их перевод в JSON и обратно |
| `stopscroll/seed.py` | Начальное состояние из раздела 5.9 |
| `stopscroll/store.py` | Атомарная запись и чтение файла состояния с резервной копией |
| `stopscroll/day.py` | Ключ дня и момент следующей смены дня |
| `stopscroll/engine.py` | Отрезки, бюджет, очередь изменений, `status` |
| `stopscroll/changes.py` | Виды изменений: проверка, деление на ослабление и ужесточение, применение |
| `stopscroll/blocklist.py` | Список заблокированных доменов, текст конфигурации `dnsmasq` и сценария `nft` |
| `stopscroll/system.py` | Приведение машины к должному состоянию, карантин, удаление |
| `stopscroll/doctor.py` | Проверки живой системы |
| `stopscroll/daemon.py` | Сокет, цикл, связка ядра и системного слоя |
| `stopscroll/cli.py` | Разбор команд владельца, вывод |
| `stopscroll/notify.py` | Уведомления рабочего стола |
| `system/*.service`, `system/*.timer` | Службы и сторож |
| `install.sh`, `uninstall.sh` | Установка и снятие |
| `README.md` | Как пользоваться, аварийное восстановление |
| `tests/test_<модуль>.py` | Тесты соответствующего модуля |

---

### Задача 1: Модель состояния, начальные списки, хранилище

**Файлы:** создать `stopscroll/__init__.py`, `stopscroll/model.py`, `stopscroll/seed.py`, `stopscroll/store.py`, `tests/__init__.py`, `tests/test_model.py`, `tests/test_store.py`, `.gitignore`.

**Интерфейсы — даёт:**

```python
# model.py
class StateError(Exception): ...

@dataclass
class Site:
    mode: str                 # "blocked" | "limited"
    domains: list[str]
    exceptions: list[str]
    budget_min: int           # 0 для blocked

@dataclass
class Session:
    segment_start: float      # время стены (epoch), с которого идёт несписанное время
    ends: float               # время стены
    warned: bool = False

@dataclass
class Pending:
    id: int
    change: dict
    remaining_sec: float
    summary: str

@dataclass
class State:
    owner_uid: int
    sites: dict[str, Site]
    doh_domains: list[str]
    day: str                              # "2026-11-15"
    version: int = 1
    delay_min: int = 45
    reset_hour: int = 4
    default_session_min: int = 15
    spent_sec: dict[str, int] = field(default_factory=dict)
    bonus_min: dict[str, int] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)
    pending: list[Pending] = field(default_factory=list)
    next_id: int = 1
    pause_until: float | None = None
    boot_id: str = ""
    boot_saved: float = 0.0
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "State": ...   # StateError при чужой версии или неполных данных

# seed.py
DOH_DOMAINS: list[str]
def initial_state(owner_uid: int, day: str) -> State: ...

# store.py
def save(state: State, path: str) -> None: ...        # tmp → fsync → прежний файл в path+".bak" → rename; права 0600
def load(path: str) -> State | None: ...              # path, затем path+".bak"; None, если оба негодны
```

- [x] **Шаг 1.** `git init` в корне, `.gitignore` с `__pycache__/`. Закоммитить уже лежащие `docs/`.

- [x] **Шаг 2. Написать падающие тесты.**

```python
# tests/test_model.py
def test_roundtrip(self):
    s = initial_state(1000, "2026-11-15")
    s.sessions["youtube"] = Session(100.0, 700.0, True)
    s.pending.append(Pending(1, {"kind": "bonus", "site": "x", "minutes": 10}, 2700.0, "x: +10 мин"))
    self.assertEqual(State.from_dict(json.loads(json.dumps(s.to_dict()))), s)

def test_seed_matches_spec(self):
    s = initial_state(1000, "2026-11-15")
    self.assertEqual({k for k, v in s.sites.items() if v.mode == "blocked"},
                     {"twitch", "liquipedia", "kick", "instagram"})
    self.assertEqual(s.sites["twitch"].domains,
                     ["twitch.tv", "ttvnw.net", "jtvnw.net", "twitchcdn.net", "ext-twitch.tv"])
    self.assertEqual(s.sites["instagram"].domains,
                     ["instagram.com", "cdninstagram.com", "instagr.am", "ig.me"])
    yt = s.sites["youtube"]
    self.assertEqual((yt.mode, yt.budget_min, yt.exceptions), ("limited", 45, ["accounts.youtube.com"]))
    self.assertEqual(yt.domains, ["youtube.com", "youtu.be", "googlevideo.com", "youtube-nocookie.com",
                                  "youtubei.googleapis.com", "youtube.googleapis.com"])
    self.assertEqual((s.sites["x"].domains, s.sites["x"].budget_min), (["x.com", "twitter.com"], 30))
    self.assertEqual((s.delay_min, s.reset_hour, s.default_session_min), (45, 4, 15))
    self.assertIn("use-application-dns.net", s.doh_domains)
    self.assertIn("dns.google", s.doh_domains)

def test_from_dict_rejects_other_version(self):   # version=2 → StateError
def test_from_dict_rejects_missing_sites(self):   # нет ключа "sites" → StateError

# tests/test_store.py  (всё в tempfile.TemporaryDirectory)
def test_save_sets_mode_0600(self):               # stat(path).st_mode & 0o777 == 0o600
def test_second_save_keeps_bak(self):             # после двух save path+".bak" читается как первое состояние
def test_load_falls_back_to_bak(self):            # в path записать b"{" → load возвращает состояние из .bak
def test_load_returns_none_when_both_bad(self):   # оба файла битые или отсутствуют → None
```

`DOH_DOMAINS` — список из раздела 5.4, слой 2, включая `use-application-dns.net`.

- [x] **Шаг 3.** Запустить тесты, убедиться, что падают на отсутствии модулей.
- [x] **Шаг 4.** Реализовать `model.py`, `seed.py`, `store.py`.
- [x] **Шаг 5.** Запустить тесты: все проходят.
- [x] **Шаг 6.** Коммит: `feat: state model, seed lists, atomic store`.

---

### Задача 2: Дни, отрезки, бюджет

**Файлы:** создать `stopscroll/day.py`, `stopscroll/engine.py`, `tests/test_day.py`, `tests/test_engine_sessions.py`.

**Интерфейсы — берёт:** `State`, `Site`, `Session`, `initial_state` из задачи 1.

**Интерфейсы — даёт:**

```python
# day.py
def day_key(ts: float, reset_hour: int, tz: tzinfo | None = None) -> str: ...    # дата (местное время − reset_hour часов)
def next_reset(ts: float, reset_hour: int, tz: tzinfo | None = None) -> float: ...

# engine.py
WARN_BEFORE_SEC = 120

class EngineError(Exception): ...        # текст на русском, показывается владельцу как есть

@dataclass
class Event:
    kind: str        # session_ending | session_closed | change_applied | change_dropped | uninstall
    site: str = ""
    text: str = ""

class Engine:
    def __init__(self, state: State, tz: tzinfo | None = None): ...
    def remaining_sec(self, site: str, now: float) -> int: ...
    def open(self, site: str, minutes: int | None, now: float) -> dict: ...
        # {"ends": float, "granted_min": int, "capped": bool, "extended": bool}
    def close(self, site: str, now: float, reason: str = "manual") -> list[Event]: ...
    def tick(self, now: float, boot: float, boot_id: str) -> list[Event]: ...
    def status(self, now: float) -> dict: ...
```

Правила, которые тесты не определяют сами:

- `remaining_sec` = бюджет + добавка − `spent_sec` − время текущего открытого отрезка от `segment_start`; не ниже нуля.
- `close` списывает `ceil(max(0, now − segment_start) / 60) * 60` секунд.
- `tick` при смене дня: `day` меняется, `spent_sec` и `bonus_min` обнуляются, у открытых отрезков `segment_start` переносится на момент смены дня. Смена происходит, только если новый ключ дня строго больше сохранённого.
- `tick` закрывает отрезок, если `now >= ends` (причина `expired`) или остаток бюджета стал нулём (причина `budget`).
- `minutes`: целое от 1 до 1440, иначе `EngineError`.
- `status` возвращает:

```python
{"blocked": ["twitch", "liquipedia", "kick", "instagram"],          # в порядке хранения в state.sites
 "limited": [{"site": "youtube", "open": True, "ends": 1.0, "left_sec": 660,
              "spent_min": 24, "budget_min": 45, "bonus_min": 0}],   # spent_min включает идущий отрезок, вверх до минуты
 "pending": [{"id": 3, "summary": "youtube: бюджет 45 → 60 мин", "eta": 1.0}],
 "delay_min": 45, "reset_hour": 4, "pause_until": None}
```

- [x] **Шаг 1. Написать падающие тесты.** Во всех тестах `tz=timezone.utc`, `T = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc).timestamp()`.

```python
# tests/test_day.py
def test_before_reset_is_previous_day(self):   # 2026-11-16 03:59 UTC, reset_hour=4 → "2026-11-15"
def test_at_reset_is_new_day(self):            # 2026-11-16 04:00 UTC → "2026-11-16"
def test_next_reset(self):                     # от 2026-11-15 12:00 → timestamp 2026-11-16 04:00 UTC

# tests/test_engine_sessions.py
def test_open_and_early_close_charges_elapsed_rounded_up(self):
    r = e.open("youtube", 20, T)
    self.assertEqual((r["ends"], r["granted_min"], r["capped"], r["extended"]), (T + 1200, 20, False, False))
    e.close("youtube", T + 170)
    self.assertEqual(st.spent_sec["youtube"], 180)

def test_open_default_is_15_min(self):         # open("youtube", None, T)["ends"] == T + 900
def test_open_capped_by_remaining(self):       # spent 35 мин из 45, просим 20 → granted_min 10, capped True
def test_open_refuses_when_exhausted(self):    # spent 45 мин → EngineError, в тексте есть "04:00"
def test_open_blocked_site_refused(self):      # open("twitch", 5, T) → EngineError
def test_open_unknown_site_refused(self):      # open("nope", 5, T) → EngineError
def test_open_rejects_bad_minutes(self):       # 0, -5, 1441 → EngineError
def test_reopen_extends(self):                 # open 10 в T, open 5 в T+60 → ends == T + 900, extended True
def test_extend_capped_by_remaining(self):     # бюджет 45: open 40, затем open 20 → ends == T + 2700
def test_sites_have_separate_budgets(self):    # трата youtube не меняет remaining_sec("x")
def test_warning_fires_once(self):
    e.open("youtube", 10, T)
    self.assertEqual([ev.kind for ev in e.tick(T + 480, 0, "b")], ["session_ending"])
    self.assertEqual(e.tick(T + 490, 0, "b"), [])

def test_expiry_closes_and_charges(self):      # tick(T+600) → session_closed/expired, spent 600, сессии нет
def test_overrun_while_daemon_down_is_charged(self):
    e.open("youtube", 20, T)
    e.tick(T + 1500, 0, "b")
    self.assertEqual(st.spent_sec["youtube"], 1500)

def test_day_rollover_resets_usage(self):      # spent 45 мин; tick в 04:00 след. дня → spent == {}, day обновлён
def test_session_across_rollover_charges_only_new_day_part(self):
    # open в 03:50, tick в 04:00 (смена дня), close в 04:03 → spent_sec["youtube"] == 180
def test_clock_back_does_not_reset_day(self):  # state.day="2026-11-15"; tick с now за 2026-11-14 → day и spent прежние
def test_clock_back_does_not_go_negative(self):# open в T, close в T-300 → spent 0, без исключения
def test_status_shape(self):                   # ключи и значения как в образце выше
```

- [x] **Шаг 2.** Запустить, убедиться в падении.
- [x] **Шаг 3.** Реализовать `day.py` и `engine.py` (очередь изменений в `tick` и `status["pending"]` пока пустые — их добавит задача 3).
- [x] **Шаг 4.** Запустить: все тесты задач 1–2 проходят.
- [x] **Шаг 5.** Коммит: `feat: sessions and daily budget`.

---

### Задача 3: Изменения правил и очередь с задержкой

**Файлы:** создать `stopscroll/changes.py`, `tests/test_changes.py`, `tests/test_engine_queue.py`; изменить `stopscroll/engine.py`.

**Интерфейсы — берёт:** `Engine`, `Event`, `EngineError` из задачи 2.

**Интерфейсы — даёт:**

```python
# changes.py
class ChangeError(Exception): ...
SITE_RE = r"^[a-z][a-z0-9]{0,23}$"
def normalize_domain(raw: str) -> str: ...
def classify(state: State, change: dict) -> str: ...      # "tighten" | "loosen"; ChangeError, если неверно или ничего не меняет
def apply(state: State, change: dict, now: float) -> None: ...   # ChangeError, если больше неприменимо
def summarize(state: State, change: dict) -> str: ...

# engine.py — добавить
class Engine:
    def submit(self, change: dict, now: float) -> dict: ...
        # {"applied": True, "summary": str, "events": list[Event]}
        # {"applied": False, "id": int, "eta": float, "summary": str}
    def cancel(self, change_id: int) -> str: ...           # возвращает summary; EngineError, если номера нет
```

Виды изменений (`change["kind"]`) и их класс:

| kind | поля | класс |
|---|---|---|
| `add_site` | `site`, `mode`, `domains`, `budget_min` | ужесточение |
| `remove_site` | `site` | ослабление |
| `add_domain` | `site`, `domain` | ужесточение |
| `remove_domain` | `site`, `domain` | ослабление; последний домен убрать нельзя — `ChangeError` |
| `add_exception` | `site`, `domain` | ослабление |
| `remove_exception` | `site`, `domain` | ужесточение |
| `set_mode` | `site`, `mode`, `budget_min` | `limited`→`blocked` ужесточение; `blocked`→`limited` ослабление |
| `set_budget` | `site`, `minutes` | меньше — ужесточение, больше — ослабление |
| `bonus` | `site`, `minutes` | ослабление; прибавляется к `bonus_min` дня, в который применилось |
| `set_delay` | `minutes` (1…1440) | больше — ужесточение, меньше — ослабление |
| `pause` | `minutes` | ослабление; `pause_until = now + minutes*60` |
| `uninstall` | — | ослабление; при применении `Engine` выдаёт `Event("uninstall")` |

Правила:

- `normalize_domain`: нижний регистр; убрать схему, путь, порт, точку в конце, ведущее `www.`; не-ASCII перевести в punycode. `ChangeError` для пустой строки, одной метки (`com`), IP-адреса, недопустимых символов, длины больше 253.
- `submit`: ужесточение применяется сразу; ослабление встаёт в очередь с `remaining_sec = state.delay_min * 60` (задержка на момент запроса).
- `tick`: если `boot_id` совпадает с сохранённым, из `remaining_sec` каждого ожидающего вычитается `boot − state.boot_saved`; при другом `boot_id` ничего не вычитается. Затем `state.boot_id`, `state.boot_saved` обновляются. Изменения с `remaining_sec <= 0` применяются по порядку номеров: успех — `Event("change_applied", text=summary)`, `ChangeError` — `Event("change_dropped", text=...)`.
- После любого применения `Engine` закрывает отрезки, потерявшие основание: сайт исчез или стал `blocked` (причина `rules`), остаток бюджета стал нулём (причина `budget`).
- `eta` = `now + remaining_sec`.

- [x] **Шаг 1. Написать падающие тесты.**

```python
# tests/test_changes.py
def test_normalize(self):
    for raw, want in [("https://www.Twitch.tv/directory", "twitch.tv"), ("Kick.com.", "kick.com"),
                      ("m.youtube.com:443", "m.youtube.com"), ("пример.рф", "xn--e1afmkfd.xn--p1ai")]:
        self.assertEqual(normalize_domain(raw), want)

def test_normalize_rejects(self):              # "", "com", "1.2.3.4", "a b.com", "x"*254 → ChangeError
def test_classify_table(self):                 # по одной проверке на каждую строку таблицы выше, включая обе стороны set_mode, set_budget, set_delay
def test_classify_noop_is_error(self):         # set_budget youtube 45 при бюджете 45 → ChangeError
def test_classify_unknown_kind_or_site(self):  # {"kind": "boom"}, set_budget для "nope" → ChangeError
def test_add_site_rejects_bad_name(self):      # site="My Site" → ChangeError
def test_add_site_rejects_existing(self):      # add_site "twitch" → ChangeError
def test_remove_last_domain_rejected(self):    # remove_domain liquipedia liquipedia.net → ChangeError
def test_apply_bonus_adds_to_today(self):      # bonus youtube 30 → state.bonus_min["youtube"] == 30
def test_summarize_budget(self):               # set_budget youtube 60 → "youtube: бюджет 45 → 60 мин"

# tests/test_engine_queue.py
def test_tighten_applies_immediately(self):    # submit set_budget youtube 30 → applied True, budget_min == 30, pending пуст
def test_loosen_is_queued_with_delay(self):
    r = e.submit({"kind": "set_budget", "site": "youtube", "minutes": 60}, T)
    self.assertEqual((r["applied"], r["id"], r["eta"]), (False, 1, T + 2700))
    self.assertEqual(st.sites["youtube"].budget_min, 45)

def test_queue_applies_after_boottime_elapses(self):
    e.tick(T, 100.0, "b"); e.submit(loosen, T)
    self.assertEqual(e.tick(T + 10, 2799.0, "b"), [])
    self.assertEqual([ev.kind for ev in e.tick(T + 20, 2801.0, "b")], ["change_applied"])
    self.assertEqual(st.sites["youtube"].budget_min, 60)

def test_wall_clock_jump_does_not_shorten_delay(self):   # tick(T + 86400, boot=110.0) → изменение всё ещё в очереди
def test_reboot_keeps_remaining(self):                   # tick с boot_id "c" и boot=5.0 → remaining_sec не уменьшился
def test_lowering_delay_waits_old_delay(self):           # submit set_delay 5 → eta == T + 2700
def test_cancel(self):                                   # cancel(1) убирает из очереди; cancel(99) → EngineError
def test_stale_pending_is_dropped_others_apply(self):
    # в очереди: #1 set_budget youtube 60 и #2 set_budget x 40; затем submit set_mode youtube blocked (применяется сразу)
    # по истечении задержки tick даёт kinds == ["change_dropped", "change_applied"], x.budget_min == 40
def test_tighten_closes_open_session(self):              # open youtube; submit set_mode youtube blocked → events содержат session_closed/rules
def test_budget_cut_below_spent_closes_session(self):    # spent 20 мин, отрезок открыт; set_budget youtube 10 → session_closed/budget
def test_uninstall_emits_event_after_delay(self):        # после задержки tick содержит Event kind "uninstall"
def test_status_lists_pending(self):                     # status(T)["pending"] == [{"id": 1, "summary": ..., "eta": T + 2700}]
```

- [x] **Шаг 2.** Запустить, убедиться в падении.
- [x] **Шаг 3.** Реализовать `changes.py`, дополнить `engine.py`.
- [x] **Шаг 4.** Запустить: все тесты задач 1–3 проходят.
- [x] **Шаг 5.** Коммит: `feat: rule changes with delayed loosening`.

---

### Задача 4: Список блокировки, конфигурация dnsmasq и nftables

**Файлы:** создать `stopscroll/blocklist.py`, `tests/test_blocklist.py`.

**Интерфейсы — даёт:**

```python
DNS_PORT = 5354
QUARANTINE_SEC = 120
TABLE = "stopscroll"
RESOLV_FILE = "/run/NetworkManager/no-stub-resolv.conf"

def set_names(site: str) -> tuple[str, str]: ...              # ("site_youtube4", "site_youtube6")
def blocked_domains(state: State, now: float) -> list[str]: ...   # по алфавиту, без повторов
def render_dnsmasq(state: State, now: float) -> str: ...
def render_nft(state: State, dns_uid: int, resolved_uid: int) -> str: ...
```

Правила:

- `blocked_domains` = `doh_domains` всегда + домены сайтов `blocked` + домены сайтов `limited` без открытого отрезка. Пока `pause_until > now`, домены сайтов не входят; `doh_domains` остаются.
- `render_dnsmasq` выдаёт, в таком порядке: постоянную шапку, по строке `address=/<домен>/` на каждый заблокированный домен, по строке `server=/<исключение>/#` на каждое исключение сайта, по одной строке `nftset=` на каждый сайт `limited`. Шапка:

```
port=5354
listen-address=127.0.0.1
bind-interfaces
no-hosts
resolv-file=/run/NetworkManager/no-stub-resolv.conf
cache-size=1000
```

- Строка `nftset` для youtube: `nftset=/youtube.com/youtu.be/googlevideo.com/youtube-nocookie.com/youtubei.googleapis.com/youtube.googleapis.com/4#inet#stopscroll#site_youtube4,6#inet#stopscroll#site_youtube6`.
- `render_nft` выдаёт сценарий для `nft -f -`, который можно применять повторно, не теряя содержимого наборов. Для начального состояния и uid 991, 992 — ровно такой текст (наборы сайтов по алфавиту):

```
add table inet stopscroll
add set inet stopscroll quarantine4 { type ipv4_addr; flags timeout; }
add set inet stopscroll quarantine6 { type ipv6_addr; flags timeout; }
add set inet stopscroll site_x4 { type ipv4_addr; }
add set inet stopscroll site_x6 { type ipv6_addr; }
add set inet stopscroll site_youtube4 { type ipv4_addr; }
add set inet stopscroll site_youtube6 { type ipv6_addr; }
add chain inet stopscroll output { type filter hook output priority filter; policy accept; }
flush chain inet stopscroll output
add rule inet stopscroll output oifname "lo" accept
add rule inet stopscroll output ip daddr @quarantine4 meta l4proto tcp reject with tcp reset
add rule inet stopscroll output ip daddr @quarantine4 reject
add rule inet stopscroll output ip6 daddr @quarantine6 meta l4proto tcp reject with tcp reset
add rule inet stopscroll output ip6 daddr @quarantine6 reject
add rule inet stopscroll output meta l4proto { tcp, udp } th dport { 53, 853 } meta skuid != { 991, 992 } reject
```

- [x] **Шаг 1. Написать падающие тесты.**

```python
def test_blocked_when_no_session(self):         # в списке twitch.tv, youtube.com, x.com, dns.google, use-application-dns.net
def test_open_session_unblocks_only_that_site(self):   # sessions["youtube"] есть → нет youtube.com и googlevideo.com, есть x.com
def test_pause_lifts_sites_but_not_doh(self):   # pause_until = T+60 → нет twitch.tv, есть dns.google
def test_pause_expired_blocks_again(self):      # pause_until = T-1 → twitch.tv есть
def test_dnsmasq_header_and_lines(self):
    text = render_dnsmasq(st, T)
    self.assertTrue(text.startswith("port=5354\nlisten-address=127.0.0.1\nbind-interfaces\n"))
    self.assertIn("address=/twitch.tv/\n", text)
    self.assertIn("server=/accounts.youtube.com/#\n", text)
    self.assertIn(YOUTUBE_NFTSET_LINE + "\n", text)       # строка из правил выше
def test_dnsmasq_keeps_nftset_when_open(self):  # отрезок открыт → address=/youtube.com/ нет, nftset-строка есть
def test_exception_only_for_blocked_site(self): # отрезок youtube открыт → строки server=/accounts.youtube.com/# нет
def test_nft_exact_text(self):                  # render_nft(seed, 991, 992) == текст из правил выше
def test_set_names(self):                       # set_names("x") == ("site_x4", "site_x6")
```

- [x] **Шаг 2.** Запустить, убедиться в падении.
- [x] **Шаг 3.** Реализовать `blocklist.py`.
- [x] **Шаг 4.** Запустить: все тесты проходят.
- [x] **Шаг 5. Проверить текст настоящими программами** (систему не меняет). Записать вывод `render_dnsmasq` и `render_nft` для начального состояния в файлы каталога `mktemp -d` и выполнить:

```bash
dnsmasq --test --conf-file="$D/dnsmasq.conf"      # ожидается: "dnsmasq: syntax check OK."
sudo nft -c -f "$D/stopscroll.nft"                # ожидается: пустой вывод, код 0
```

Для `render_nft` подставить `dns_uid=$(id -u nobody)`, `resolved_uid=$(id -u systemd-resolve)`. Если любая из проверок ругается, исправить `blocklist.py` и тест с точным текстом вместе.

- [x] **Шаг 6.** Коммит: `feat: blocklist, dnsmasq and nftables rendering`.

---

### Задача 5: Системный слой

**Файлы:** создать `stopscroll/system.py`, `tests/test_system.py`.

**Интерфейсы — берёт:** `render_dnsmasq`, `render_nft`, `set_names`, `QUARANTINE_SEC`, `DNS_PORT` из задачи 4.

**Интерфейсы — даёт:**

```python
DNS_CONF = "/var/lib/stopscroll/dnsmasq.conf"
RESOLVED_DROPIN = "/etc/systemd/resolved.conf.d/stopscroll.conf"
FIREFOX_POLICY = "/etc/firefox/policies/policies.json"
CHROME_POLICIES = ["/etc/opt/chrome/policies/managed/stopscroll.json",
                   "/etc/chromium/policies/managed/stopscroll.json"]
DNS_UNIT = "stopscroll-dns.service"

class Runner:
    def __init__(self, root: str = "/"): ...
    def path(self, p: str) -> pathlib.Path: ...                      # p внутри root
    def run(self, argv: list[str], input: str | None = None) -> subprocess.CompletedProcess: ...   # без исключения при ненулевом коде
    def uid(self, name: str) -> int: ...

class System:
    def __init__(self, runner: Runner): ...
    def sync(self, state: State, now: float) -> list[str]: ...       # что исправлено или не удалось; никогда не бросает
    def quarantine(self, site: str) -> None: ...                     # никогда не бросает
    def uninstall(self) -> None: ...
```

Что делает `sync`, по порядку. Каждый пункт сначала сравнивает, меняет только при расхождении и добавляет строку в результат:

1. **nftables.** Запомнить, существует ли таблица (`nft list table inet stopscroll`, код возврата). Применить `render_nft` через `nft -f -`.
2. **Конфигурация DNS.** Если `DNS_CONF` отличается от `render_dnsmasq` — записать, выполнить `systemctl restart stopscroll-dns.service` и `resolvectl flush-caches`. Если таблицы на шаге 1 не было — перезапустить DNS в любом случае.
3. **Служба DNS.** `systemctl is-active` не `active` → `systemctl start`.
4. **systemd-resolved.** Файл `RESOLVED_DROPIN` должен быть ровно `[Resolve]\nDNS=127.0.0.1:5354\nDomains=~.\n`; при записи — `systemctl restart systemd-resolved`.
5. **Политики.** В `FIREFOX_POLICY` ключ `policies.DNSOverHTTPS` должен быть `{"Enabled": false, "Locked": true}`; остальные ключи файла сохраняются; нечитаемый файл переименовывается в `policies.json.stopscroll-bak` и создаётся заново. Каждый файл `CHROME_POLICIES` должен быть `{"DnsOverHttpsMode": "off"}`.

Ошибка на любом пункте (ненулевой код, `OSError`) даёт строку `"сбой: <что>: <почему>"` в результате; остальные пункты выполняются.

`quarantine(site)`: для каждого из двух наборов сайта прочитать элементы (`nft -j list set inet stopscroll <имя>`; в JSON это `nftables[].set.elem`, ключа может не быть), добавить их в `quarantine4`/`quarantine6` с `timeout 120s`, затем `nft flush set`.

`uninstall()`: `systemd-run --unit=stopscroll-uninstall /usr/local/lib/stopscroll/uninstall.sh`.

- [x] **Шаг 1. Написать падающие тесты.** `FakeRunner(Runner)` с `root` во временном каталоге записывает все `argv` в список и отдаёт заранее заданные ответы по первому слову команды.

```python
def test_first_sync_writes_everything(self):
    # после sync: существуют DNS_CONF, RESOLVED_DROPIN, FIREFOX_POLICY, оба CHROME_POLICIES;
    # среди команд есть ["nft","-f","-"], ["systemctl","restart","stopscroll-dns.service"], ["resolvectl","flush-caches"]
def test_second_sync_changes_nothing(self):     # второй sync → [] и ни одного restart в командах
def test_changed_blocklist_restarts_dns_and_flushes(self):   # открыть отрезок в state → sync → restart DNS и flush-caches
def test_deleted_policy_is_restored_and_reported(self):      # удалить chrome-файл → sync вернул 1 строку, файл на месте
def test_firefox_policy_is_merged(self):
    # заранее: {"policies": {"DisableTelemetry": true}} → после sync оба ключа на месте
def test_unreadable_firefox_policy_is_backed_up(self):       # заранее "не json" → есть .stopscroll-bak, новый файл корректен
def test_resolved_dropin_exact_text(self):
def test_dns_not_active_is_started(self):       # is-active отвечает "inactive" → есть ["systemctl","start","stopscroll-dns.service"]
def test_missing_table_forces_dns_restart(self):# nft list table → код 1 → restart DNS даже при неизменном DNS_CONF
def test_command_failure_does_not_raise(self):  # nft -f отвечает кодом 1 → sync вернул строку с "сбой", остальные файлы записаны
def test_unwritable_file_does_not_raise(self):  # каталог политик заменён файлом → строка с "сбой", без исключения
def test_quarantine_moves_elements(self):
    # list set site_youtube4 → elem ["142.250.1.1","142.250.1.2"]; site_youtube6 → без elem
    # команды содержат: nft add element inet stopscroll quarantine4 { 142.250.1.1 timeout 120s, 142.250.1.2 timeout 120s }
    # и nft flush set для обоих наборов сайта
def test_quarantine_survives_nft_error(self):   # nft -j отвечает кодом 1 → без исключения
```

- [x] **Шаг 2.** Запустить, убедиться в падении.
- [x] **Шаг 3.** Реализовать `system.py`.
- [x] **Шаг 4.** Запустить: все тесты проходят.
- [x] **Шаг 5.** Коммит: `feat: system layer with self-repair and quarantine`.

---

### Задача 6: Служба

**Файлы:** создать `stopscroll/daemon.py`, `stopscroll/doctor.py`, `tests/test_daemon.py`, `tests/test_doctor.py`.

**Интерфейсы — берёт:** `Engine`, `Event`, `EngineError` (2, 3); `ChangeError` (3); `System`, `Runner` (5); `store.load`, `store.save`, `initial_state`, `day_key`.

**Интерфейсы — даёт:**

```python
# daemon.py
SOCKET = "/run/stopscroll/stopscroll.sock"
STATE = "/var/lib/stopscroll/state.json"
OWNER_FILE = "/etc/stopscroll/owner-uid"      # одно число; пишет install.sh
TICK_SEC = 1.0
SYNC_EVERY_SEC = 60.0

def bootstrap(state_path: str, owner_uid: int | None, now: float) -> State: ...
    # load; если None — initial_state(owner_uid, day_key(now, 4)); если нет ни состояния, ни owner_uid — SystemExit с текстом

class Daemon:
    def __init__(self, engine: Engine, system, save: Callable[[], None], doctor: Callable[[], list]): ...
    def handle(self, req: object, peer_uid: int, now: float) -> dict: ...
    def tick(self, now: float, boot: float, boot_id: str) -> list[dict]: ...    # события для подписчиков
    async def serve(self, socket_path: str) -> None: ...

def main(argv: list[str] | None = None) -> int: ...    # owner_uid читается из OWNER_FILE; флаг --init: засеять состояние, один sync, выйти

# doctor.py
def run_checks(runner: Runner, state: State) -> list[dict]: ...     # [{"name": str, "ok": bool, "detail": str}]
```

Протокол: клиент шлёт одну строку JSON, получает одну строку JSON. Успех — `{"ok": true, ...}`, отказ — `{"ok": false, "error": "<текст на русском>"}`.

| `cmd` | поля запроса | поля ответа |
|---|---|---|
| `status` | — | `status` (словарь из `Engine.status`) |
| `open` | `site`, `minutes` (целое или `null`) | поля из `Engine.open` |
| `close` | `site` | — |
| `change` | `change` | поля из `Engine.submit` без `events` |
| `cancel` | `id` | `summary` |
| `doctor` | — | `checks` |
| `subscribe` | — | соединение остаётся открытым; служба шлёт события строками JSON |

Правила:

- Сокет создаётся с правами 0666; служба читает uid собеседника (`SO_PEERCRED`) и отвечает `{"ok": false, "error": "нет доступа"}` всем, кроме uid 0 и `state.owner_uid`.
- `handle` никогда не бросает: не-словарь, неизвестная `cmd`, отсутствующее или нечисловое поле, `EngineError`, `ChangeError` — всё превращается в отказ.
- После каждой команды, изменившей состояние, и после каждого `tick`, давшего события: `save()`, для каждого `session_closed` — `system.quarantine(site)`, затем `system.sync(...)`. Событие `uninstall` → `system.uninstall()`.
- Полная сверка `system.sync` идёт ещё и раз в `SYNC_EVERY_SEC`; её непустой результат пишется в журнал. Если в очереди есть изменения, `save()` выполняется не реже раза в 60 секунд.
- События подписчикам: `{"event": kind, "site": site, "text": text}`.
- Конец паузы и другие изменения списка, наступающие без события, подхватывает минутная сверка; отдельного механизма для них нет.
- Время: `time.time()`, `time.clock_gettime(time.CLOCK_BOOTTIME)`, `boot_id` из `/proc/sys/kernel/random/boot_id`.

Проверки `run_checks`, по одной записи на каждую:
`stopscroll-dns.service` активна; `stopscroll-watchdog.timer` активен; глобальный DNS `systemd-resolved` — `127.0.0.1:5354` с доменом `~.` (по `resolvectl status`); ни у одного интерфейса нет домена `~.` (иначе «блокировка не действует: похоже на VPN»); `twitch.tv` не разрешается (`resolvectl query`); `example.com` разрешается; прямой запрос `dig +time=2 +tries=1 @1.1.1.1 example.com` не проходит; таблица `inet stopscroll` есть; файлы политик на месте и верны.

- [x] **Шаг 1. Написать падающие тесты.** `FakeSystem` считает вызовы `sync`, `quarantine`, `uninstall`.

```python
# tests/test_daemon.py
def test_bootstrap_seeds_blocked_state_when_store_empty(self):
    # пустой каталог, owner_uid=1000 → State с sites["twitch"].mode == "blocked"
def test_bootstrap_uses_bak_when_main_corrupt(self):
def test_bootstrap_without_state_and_owner_exits(self):   # SystemExit
def test_stranger_is_refused(self):            # handle({"cmd":"status"}, 1001, T) == {"ok": False, "error": "нет доступа"}
def test_root_and_owner_allowed(self):
def test_status(self):                         # reply["ok"] и reply["status"]["delay_min"] == 45
def test_open_saves_and_syncs(self):           # после open: save вызван 1 раз, sync вызван 1 раз
def test_close_quarantines(self):              # open, затем close → quarantine вызван с "youtube"
def test_expiry_in_tick_quarantines_and_returns_event(self):
    # open 10 мин; d.tick(T+600, …) → [{"event":"session_closed","site":"youtube",…}], quarantine вызван
def test_loosen_change_is_queued(self):        # reply["applied"] is False, есть "id" и "eta"
def test_cancel(self):
def test_uninstall_event_calls_system(self):
def test_malformed_requests_get_error_not_exception(self):
    for req in [None, "x", [], {}, {"cmd": "boom"}, {"cmd": "open"}, {"cmd": "open", "site": "youtube", "minutes": "ten"},
                {"cmd": "cancel", "id": "one"}, {"cmd": "change", "change": 5}]:
        self.assertFalse(d.handle(req, 1000, T)["ok"])
def test_engine_error_text_reaches_client(self):          # open twitch → error — текст EngineError
def test_socket_roundtrip(self):               # serve во временном каталоге; клиент шлёт status, получает ok (peer uid — свой, owner_uid = os.getuid())
def test_bad_json_line_gets_error_and_server_survives(self):   # шлём b"{oops\n", затем нормальный status на новом соединении

# tests/test_doctor.py  (FakeRunner из задачи 5)
def test_all_green(self):
def test_link_with_catchall_domain_is_flagged(self):      # в выводе resolvectl у интерфейса "DNS Domain: ~." → проверка ok False
def test_direct_dns_reachable_is_flagged(self):           # dig отвечает кодом 0 → проверка ok False
def test_check_command_missing_is_reported_not_raised(self):
```

- [x] **Шаг 2.** Запустить, убедиться в падении.
- [x] **Шаг 3.** Реализовать `daemon.py` и `doctor.py`.
- [x] **Шаг 4.** Запустить: все тесты проходят.
- [x] **Шаг 5.** Коммит: `feat: daemon and doctor`.

---

### Задача 7: Команда владельца и уведомитель

**Файлы:** создать `stopscroll/cli.py`, `stopscroll/notify.py`, `tests/test_cli.py`, `tests/test_notify.py`.

**Интерфейсы — берёт:** протокол и `SOCKET` из задачи 6; форму `status` из задачи 2.

**Интерфейсы — даёт:**

```python
# cli.py
def build_request(argv: list[str]) -> dict: ...          # SystemExit(2) при неверном вызове
def needs_confirmation(argv: list[str]) -> bool: ...
def render_status(status: dict, now: float, tz: tzinfo | None = None) -> str: ...
def render_reply(req: dict, reply: dict, tz: tzinfo | None = None) -> str: ...
def main(argv: list[str] | None = None) -> int: ...       # 0 успех, 1 отказ службы, 2 неверный вызов, 3 служба недоступна

# notify.py
def format_event(event: dict) -> tuple[str, str] | None: ...   # (заголовок, текст)
def main() -> int: ...
```

Команды и запросы, в которые они превращаются:

| Команда | Запрос |
|---|---|
| `stopscroll` / `stopscroll status` | `{"cmd":"status"}` |
| `stopscroll open youtube [20]` | `{"cmd":"open","site":"youtube","minutes":20}` (`null` без числа) |
| `stopscroll close youtube` | `{"cmd":"close","site":"youtube"}` |
| `stopscroll block twitch` | `change`: `set_mode` `blocked` |
| `stopscroll block games steam.com epic.com` | новый сайт → `add_site` `blocked`; существующий → по `add_domain` на домен |
| `stopscroll limit youtube 30` | `change`: `set_budget` |
| `stopscroll delay 90` | `change`: `set_delay` |
| `stopscroll request budget youtube 60` | `change`: `set_budget` |
| `stopscroll request bonus youtube 30` | `change`: `bonus` |
| `stopscroll request unblock twitch [twitch.tv]` | `remove_site` или `remove_domain` |
| `stopscroll request limit twitch 20` | `set_mode` `limited`, `budget_min` 20 |
| `stopscroll request except youtube accounts.youtube.com` | `add_exception` |
| `stopscroll request delay 15` | `set_delay` |
| `stopscroll request pause 120` | `pause` |
| `stopscroll request uninstall` | `uninstall` |
| `stopscroll cancel 3` | `{"cmd":"cancel","id":3}` |
| `stopscroll doctor` | `{"cmd":"doctor"}` |

Правила:

- Чтобы различить новый и существующий сайт в `block`, `main` сначала запрашивает `status`. `build_request` для `block` с доменами принимает список известных сайтов вторым необязательным аргументом.
- Класс изменения определяет служба. `block`, `limit`, `delay` — лишь короткая запись; если служба сочла изменение ослаблением, оно встаёт в очередь, и ответ это говорит.
- `block`, `limit`, `delay` действуют сразу, а отменяются только через задержку. Поэтому перед отправкой `main` печатает, что именно изменится, и спрашивает `Действует сразу, отменить можно только через задержку. Продолжить? [y/N]`. Флаг `--yes` пропускает вопрос; при отсутствии терминала без `--yes` — отказ с кодом 2.
- `render_status` даёт ровно такой текст для образца из задачи 2, дополненного сайтом `x` (закрыт, 0 из 30) и `now` = 14:41, `ends` = 14:52, `eta` = 15:20:

```
Закрыто:      twitch  liquipedia  kick  instagram

С лимитом:
  youtube   открыт до 14:52 (осталось 11 мин)   сегодня 24 из 45 мин
  x         закрыт                              сегодня  0 из 30 мин

Ждут применения:
  #3  youtube: бюджет 45 → 60 мин       применится в 15:20  (отмена: stopscroll cancel 3)

Задержка на ослабление: 45 мин.   Новый день в 04:00.
```

  Порядок закрытых сайтов — как пришёл в `status`. Блок «Ждут применения» опускается, если очередь пуста. При добавке в строке сайта: `сегодня 24 из 45+30 мин`. При паузе первой строкой: `ПАУЗА до 16:00 — сайты открыты.`
- `doctor` печатает по строке на проверку (`ok` / `СБОЙ`, имя, подробность) и завершает код 1, если есть сбой.
- `format_event`: `session_ending` → `("stopscroll", "youtube закроется через 2 минуты")`; `session_closed` → `"youtube закрыт: время вышло"` / `"…: бюджет на сегодня исчерпан"` / `"…: закрыт вручную"` / `"…: изменились правила"` по причине `expired` / `budget` / `manual` / `rules`; `change_applied` → `"Применено: <text>"`; `change_dropped` → `"Не применено: <text>"`; остальное → `None`.
- `notify.main`: подключиться, отправить `subscribe`, на каждое событие вызвать `notify-send -a stopscroll <заголовок> <текст>`; при обрыве — повтор через 5 секунд.

- [x] **Шаг 1. Написать падающие тесты.**

```python
# tests/test_cli.py
def test_request_table(self):                   # каждая строка таблицы выше: build_request(argv) == ожидаемый словарь
def test_block_new_vs_existing_site(self):      # known=["twitch"]: "block twitch a.com" → add_domain; "block games a.com" → add_site
def test_bad_usage_exits_2(self):               # [], ["open"], ["open","youtube","abc"], ["request","boom"], ["cancel","x"]
def test_needs_confirmation(self):              # block/limit/delay → True; с --yes → False; open/status/request → False
def test_render_status_golden(self):            # ровно текст из правил выше, tz=timezone.utc
def test_render_status_without_pending(self):   # нет строки "Ждут применения"
def test_render_status_with_bonus_and_pause(self):
def test_render_open_capped(self):              # reply capped=True, granted_min=10 → в тексте "10 мин" и "остаток бюджета"
def test_render_queued_change(self):            # applied False, eta 15:20 → "в очереди" и "15:20" и "stopscroll cancel 3"
def test_render_error(self):                    # {"ok": False, "error": "нет доступа"} → "нет доступа"
def test_main_daemon_unavailable_returns_3(self):   # сокет не существует → код 3, понятный текст в stderr

# tests/test_notify.py
def test_format_each_kind(self):                # по проверке на каждую строку правил format_event
def test_unknown_event_is_none(self):
```

- [x] **Шаг 2.** Запустить, убедиться в падении.
- [x] **Шаг 3.** Реализовать `cli.py` и `notify.py`.
- [x] **Шаг 4.** Запустить: все тесты проходят.
- [x] **Шаг 5.** Коммит: `feat: owner CLI and desktop notifier`.

---

### Задача 8: Службы systemd, установка, удаление, README

**Файлы:** создать `system/stopscroll-dns.service`, `system/stopscrolld.service`, `system/stopscroll-watchdog.service`, `system/stopscroll-watchdog.timer`, `system/stopscroll-notify.service`, `install.sh`, `uninstall.sh`, `README.md`, `tests/test_packaging.py`.

**Интерфейсы — берёт:** `python3 -m stopscroll.daemon [--init]`, файл `OWNER_FILE`, `python3 -m stopscroll.cli`, `python3 -m stopscroll.notify`; пути из задач 5 и 6.

Содержание единиц:

- `stopscroll-dns.service`: `ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --user=stopscroll-dns --conf-file=/var/lib/stopscroll/dnsmasq.conf`; `Restart=always`, `RestartSec=1`; `WantedBy=multi-user.target`.
- `stopscrolld.service`: `Environment=PYTHONPATH=/usr/local/lib/stopscroll`; `ExecStart=/usr/bin/python3 -m stopscroll.daemon`; `RuntimeDirectory=stopscroll`; `StateDirectory=stopscroll`; `StateDirectoryMode=0700`; `Restart=always`, `RestartSec=2`; `WantedBy=multi-user.target`.
- `stopscroll-watchdog.timer`: `OnBootSec=30`, `OnUnitActiveSec=60`; `WantedBy=timers.target`. Его служба (`Type=oneshot`): `ExecStart=/usr/bin/systemctl start stopscrolld.service stopscroll-dns.service`.
- `stopscroll-notify.service` (пользовательская, кладётся в `/etc/systemd/user/`): `ExecStart=/usr/bin/python3 -m stopscroll.notify` с тем же `PYTHONPATH`; `Restart=always`, `RestartSec=5`; `WantedBy=graphical-session.target`.

`install.sh` (запуск через `sudo`, повторный запуск безопасен), по порядку:

1. Отказ, если запущен не через `sudo` обычным пользователем (нужны `SUDO_UID`, `SUDO_USER`) или если порт 5354 занят чужим процессом.
2. Создать системного пользователя `stopscroll-dns` (без дома, без оболочки), если его нет.
3. Скопировать пакет `stopscroll/` и `uninstall.sh` в `/usr/local/lib/stopscroll/` (владелец root, без записи для остальных). Поставить `/usr/local/bin/stopscroll` — сценарий из двух строк, запускающий `python3 -m stopscroll.cli` с нужным `PYTHONPATH`.
4. Записать uid владельца в `/etc/stopscroll/owner-uid` (одно число). Скопировать системные единицы в `/etc/systemd/system/`, пользовательскую — в `/etc/systemd/user/`; `systemctl daemon-reload`.
5. `python3 -m stopscroll.daemon --init`: засеять состояние, если его нет, и выполнить один `sync` (он создаёт таблицу `nftables`, конфигурацию DNS, настройку `systemd-resolved`, политики и запускает `stopscroll-dns.service`).
6. `systemctl enable --now stopscroll-dns.service stopscrolld.service stopscroll-watchdog.timer`.
7. Включить уведомитель владельцу: `sudo -u "$SUDO_USER" XDG_RUNTIME_DIR=/run/user/$SUDO_UID systemctl --user enable --now stopscroll-notify.service`; неудача — предупреждение, не ошибка.
8. Подождать до 10 секунд появления сокета, выполнить `stopscroll doctor` от имени владельца. Код возврата `install.sh` = код `doctor`.

`uninstall.sh` (root): отключить и остановить таймер и обе службы; удалить `RESOLVED_DROPIN` и перезапустить `systemd-resolved`; `nft delete table inet stopscroll`; удалить оба файла `CHROME_POLICIES`; убрать ключ `DNSOverHTTPS` из `FIREFOX_POLICY`, сохранив остальное; удалить файлы единиц, `/usr/local/bin/stopscroll`, `/etc/stopscroll`, `/var/lib/stopscroll`, пользователя `stopscroll-dns`; `systemctl daemon-reload`; последним — `/usr/local/lib/stopscroll`. Каждый шаг выполняется, даже если предыдущий не удался.

`README.md`: что это и зачем (три абзаца); команды с примерами из задачи 7; как устроена задержка; известные ограничения из раздела 9; раздел «Аварийное восстановление»:

```bash
sudo systemctl disable --now stopscroll-watchdog.timer stopscrolld.service stopscroll-dns.service
sudo rm /etc/systemd/resolved.conf.d/stopscroll.conf && sudo systemctl restart systemd-resolved
sudo nft delete table inet stopscroll
```

- [x] **Шаг 1. Написать падающие тесты** (проверяют файлы как текст, систему не трогают).

```python
# tests/test_packaging.py
def test_units_parse_and_have_required_keys(self):
    # configparser(strict=False) читает каждую единицу; проверяются ExecStart, Restart, RestartSec, WantedBy из описания выше
def test_dns_unit_uses_same_conf_path_as_system_module(self):   # system.DNS_CONF встречается в ExecStart
def test_dns_unit_drops_to_dedicated_user(self):                # "--user=stopscroll-dns" в ExecStart
def test_watchdog_interval(self):                               # OnUnitActiveSec == "60"
def test_scripts_pass_shell_syntax_check(self):                 # subprocess: bash -n install.sh; bash -n uninstall.sh → код 0
def test_uninstall_removes_every_path_system_module_creates(self):
    # RESOLVED_DROPIN и оба CHROME_POLICIES встречаются в тексте uninstall.sh
def test_readme_has_emergency_section(self):                    # "Аварийное восстановление" и три команды из блока выше
```

- [x] **Шаг 2.** Запустить, убедиться в падении.
- [x] **Шаг 3.** Написать единицы, `install.sh`, `uninstall.sh`, `README.md`.
- [x] **Шаг 4.** Запустить: все тесты всех задач проходят.
- [x] **Шаг 5.** `systemd-analyze verify system/*.service system/*.timer` — без ошибок (предупреждения о ненайденном `/usr/local/lib/...` допустимы).
- [x] **Шаг 6.** Коммит: `feat: systemd units, install and uninstall scripts, README`.

---

### Задача 9: Установка на машину и приёмка

Эта задача меняет DNS рабочей машины. Выполняется в основной сессии, при владельце, после его явного «ставим». Отдельному исполнителю без владельца не передаётся.

**Файлы:** изменить `README.md` (раздел «Результат установки»); при находках — файлы, в которых найдена ошибка, вместе с их тестами.

- [x] **Шаг 1. Подготовить отход.** Открыть в отдельном терминале три команды из «Аварийного восстановления». Записать текущее состояние: `resolvectl status | head -20`, `sudo nft list tables`.
- [x] **Шаг 2. Установить.** `sudo ./install.sh`. Ожидается: `doctor` печатает все проверки `ok`, код 0. Если пропал интернет и `doctor` не зелёный — выполнить отход, разобраться по `journalctl -u stopscrolld -u stopscroll-dns -b`, исправить, повторить.
- [x] **Шаг 3. Самостоятельно проверить браузеры.** По последующему указанию владельца проверки выполняются агентом в новых изолированных процессах Firefox и Chrome; личные окна и несохранённая работа не закрываются.
- [ ] **Шаг 4. Пройти приёмку из раздела 8 спецификации, пункты 1–15, по одному.** Проверки командной строки:

```bash
for h in twitch.tv clips.twitch.tv liquipedia.net kick.com www.instagram.com youtube.com x.com; do
  resolvectl query "$h" >/dev/null 2>&1 && echo "ОТКРЫТ $h" || echo "закрыт $h"; done     # все «закрыт»
resolvectl query example.com accounts.youtube.com                                          # оба разрешаются
dig +time=2 +tries=1 @8.8.8.8 twitch.tv                                                    # не проходит
docker run --rm alpine nslookup example.com                                                # разрешается
stopscroll open youtube 10 && sleep 3 && resolvectl query youtube.com                      # разрешается
sudo nft list set inet stopscroll site_youtube4                                            # после захода на YouTube — непустой
sudo systemctl stop stopscrolld stopscroll-dns; sleep 75; systemctl is-active stopscrolld stopscroll-dns   # active, active
```

  Пункты про браузеры (1, 4, 5, 6, 15) владелец проверяет глазами; исполнитель называет, что открыть и что должно получиться. Пункт 7: `stopscroll open youtube 10`, через 3 минуты `stopscroll close youtube`, в `status` — 3 минуты. Пункт 9: в начале приёмки выполнить `stopscroll request bonus x 5` и `stopscroll request bonus youtube 5`; второе сразу отменить и убедиться, что оно исчезло из `status`. Первое оставить ждать: задержку сократить нельзя, поэтому остальные пункты проходят, пока идут 45 минут. По их истечении добавка должна появиться в `status` сама, с уведомлением. Пункт 12: перезагрузка с открытым отрезком и изменением в очереди.
- [x] **Шаг 5. Клоак-браузер.** Исключён по указанию владельца: находится на другом устройстве. Это записано в README.
- [x] **Шаг 6. Записать результат** в `README.md`, раздел «Результат установки»: дата, итог по каждому из 15 пунктов, результат по клоак-браузеру, всё найденное сверх спецификации.
- [x] **Шаг 7.** Каждая найденная ошибка исправляется через падающий тест в задаче-владельце кода, затем `sudo ./install.sh` повторно и повтор задетых пунктов приёмки.
- [x] **Шаг 8.** Коммит: `docs: installation result and acceptance record`.

## Запись исполнения 2026-11-15

Задачи 1–8 реализованы сабагентами, каждая прошла независимое ревью; исправления общего обзора также проверены. Установка в задаче 9 заранее одобрена и выполнена основной сессией. Владелец делегировал рабочие решения и браузерные проверки исполнителю; дополнительные подтверждения не требуются. Изменения учёта, текущих проверок doctor и команды remove-exception отражены в коде, тестах и README. Полный список фактических результатов приёмки находится в README; фактическая перезагрузка и авторизация с учётными данными не выдаются за проверенные. Поэтому общий шаг 4 приёмки оставлен открытым.
