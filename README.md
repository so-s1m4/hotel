# Hotel Operations Prototype

## Презентационный сайт с Cerebras

Интерактивный 3D-отель: все 40 номеров на одном обзорном уровне, ресепшн,
помещение уборки, сообщения гостей и экран телефона управляющего. Планировка
иллюстративна; номера и состояния сохраняют связь с исходным датасетом.

```bash
npm --prefix hotel_web ci
npm --prefix hotel_web run build
.venv/bin/python -m hotel_bot
# http://127.0.0.1:8765
```

На Mac можно запустить `Start Hotel.command`. Ключ Cerebras хранится вне проекта
в `~/.config/hotel/cerebras.key` или передаётся через `CEREBRAS_API_KEY`.
Сайт передаёт извлечённые GPT-OSS-120B намерения настоящему `DecisionEngine`.
Простые задачи движок ставит в очередь автономно; телефон управляющего показывает
только решения, которым требуется согласование. Вход в занятый номер требует
отдельного согласия гостя. Ранний заезд проходит существующее планирование уборки
с проверкой ресурсов. Все действия исполняются через `MockExecutor`, без физических
эффектов и выдуманной доставки сообщений.

В интерактивном режиме комнаты — демонстрационные данные. Для исходных Quickstart
и Dev сайт открывает сохранённые полные прогоны: **Play / Pause**, движущиеся часы,
скорость, перемотка и Restart. Все события идут от первой до последней записи
нормализованного потока; счётчики и журнал ограничены выбранным моментом.
Режим **3-minute tour** показывает весь период за три минуты без повторного
вызова модели. Подготовка нового расчёта — отдельная кнопка **Prepare dataset**.
Жители в 3D показывают подтверждённую занятость; занятия и позиции иллюстративны.
**Upload ZIP** импортирует новый комплект PLATO40 из вложенных папок. После одного
**Prepare dataset** сохранённую историю можно воспроизводить повторно без модели.

Полный журнал содержит причины действий, исходные свидетельства и результат
симуляции. **Action log → Download log** выгружает записи до выбранного момента;
в конце — весь журнал. Результаты хранятся в `outputs/bot-session/` и
`outputs/bot-replays/`.
[Запуск и сценарий презентации](docs/DEMO_SITE.md).

Расширение action vocabulary для включённого `enable_guest_services=True`:
`create_service_task` означает **поставить административную задачу или передачу
предметов в очередь**, например Wi-Fi, полотенца или проверку доступа. Его audit
имеет фазу `task_queued`; это не разрешение войти, открыть дверь, выставить счёт,
отправить сотрудника или изменить оборудование. Допустимые задачи и службы
проверяет `engine.safety`; действие не резервирует сотрудника. Флаг включён в
сайте и его dataset replay, по умолчанию выключен в `DecisionEngine`, поэтому
исходный CLI ниже сохраняет прежнюю конфигурацию.

Offline прототип эксплуатации вымышленного PLATO-40: причинный replay → риск →
планирование → safety/approval → mock audit → наблюдаемая проверка → годовые выводы.

Интегрированы `ingestion/` и `engine/`: S01–S05, батарея/PV, преднагрев, роботы,
мусор и вендинг. Реальные Quickstart и Dev запускаются без API-ключа. Исторические
наблюдения не меняются, сообщения не считаются доставленными, действия не управляют
реальным зданием. Holdout ещё не использовался; точность детекции без истинных меток
не измерена.

[Разбор независимого review и оставшиеся ограничения](docs/reviews/RESOLUTION.md).
[Алгоритмы и допущения](docs/ENGINE.md). [Измеренный годовой анализ](insights.md).

## Установка и данные

Python 3.11+, команды из корня репозитория:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
# ZIP организатора в корне или --archive-dir /path/to/downloads
python scripts/prepare_data.py
python run.py --data-dir data/PLATO40_v2_quickstart --check
python -m pytest -q
```

Воспроизводимые зависимости с uv: `uv sync --frozen --extra dev`.
Архивы и распакованные наборы не хранятся в Git; [как подключить](data/README.md).
Quickstart (21 день) и Dev (365 дней) — независимые синтетические симуляции.

## Запуск и независимая проверка

```bash
python run.py --data-dir data/PLATO40_v2_dev \
  --out decisions.jsonl --audit decisions_audit.jsonl

python -m engine.validation --data-dir data/PLATO40_v2_dev \
  --decisions decisions.jsonl --audit decisions_audit.jsonl \
  --out outputs/dev_validation.json
```

В корне лежат результаты фактического Dev replay; validation JSON содержит хэши
точно проверенных решений, audit и входов. Проверка сопоставляет ID/action/time,
согласования, структурированные отмены, пересечения ресурсов/комнат, уже известную
занятость и точные дубли уведомлений. Это не сертификат отсутствия всех ошибок.

[Текущая проверка Dev](docs/reviews/reliability_dev_validation.json),
[проверка Quickstart](docs/reviews/reliability_quickstart_validation.json),
[сводка 25 проверок и хэши](docs/reviews/reliability_verification.json).

Для runtime `f8370bb` получены 342 решения Quickstart и 7 201 Dev с соответствующими
строками аудита, без ошибок валидатора и safety rejections. В корне опубликована
именно эта проверенная Dev-генерация. Все 1 026 тестов зафиксированы в репозитории
и прошли за 75,04 с; runtime-код — `f8370bb`. Два независимых Dev-процесса с `PYTHONHASHSEED=71` и `11`
дали побайтово одинаковые решения и аудит:
[воспроизводимость](docs/reviews/reliability_reproducibility.json).
[Исправления 17 групп дефектов, A–E, CLI и границы проверки](docs/reviews/HARDENING.md).

Исторический F5 snapshot сохранён отдельно: runtime `6f8cbc0`, 699 тестов,
335 решений Quickstart и 6 343 Dev. Его JSON-отчёты и парный эксперимент не заменены
новыми результатами: [отчёт F5](docs/reviews/F5.md).

CLI по умолчанию отключает модель. `--llm` явно включает настроенный parser;
при ошибке/отсутствии модели исходная метка сохраняется как непроверенный fallback.
Такой fallback не даёт приоритет на немедленную уборку; это отличается от явно
выбранного офлайн-режима. Настройки — `.env.example`;
секреты не коммитить. `--config PATH.json` переопределяет валидируемые `EngineConfig`
пороги. `--insights PATH.md` пишет сводку решений;
`--annual-insights PATH.md` после replay пишет измеренный ретроспективный отчёт
и соседний JSON. Все выходы должны находиться вне исходного data-dir.

Локальная модель: [запуск Ollama и профиля extraction](docs/LOCAL_LLM.md).
Обученные кандидаты: [измеренные результаты и ограничения](docs/MODEL_CARD.md).
Она извлекает намерения и дословные фразы; время и ограничения проверяет код.
[Сравнение 3B/8B, контрольные наборы и границы проверки](docs/reviews/LOCAL_PARSER.md).

## Покрытие

| Область | Реализовано |
|---|---|
| S01 turnaround | Известная занятость и готовность, реальные смены, условные оценки, отмены/перепланирование, позднее согласование on-call |
| S02 вода | Непрерывные дельты, сбросы/пропуски, vacancy/clean/night, строгий main residual, отдельный осторожный low-rate review и диагностика исключений |
| S03 комфорт | Длительный холод/перегрев, устойчивое восстановление с margin, HP1 running-only COP, доступные прогнозы/тарифы |
| S04 обслуживание | AHU filter/airflow против data gaps, известные work orders, смены техника и approval внешнего сервиса |
| S05 загрузка | Одновременная известная плановая загрузка, фактическая occupancy, отсутствие персонала, общие ресурсы, допустимые robot floor tasks |
| Энергия | Ограниченная battery charge/discharge по PV/опубликованным ценам, SOC/efficiency/power, vacant preheat при известном заезде и свежих условиях |
| Логистика | Ёмкость контейнера с плановыми переносами, реальные robot slots, прогноз дефицита, обычные визиты и lead time дополнительных заказов |
| Learning loop | Измерения Dev, рекомендации CLARA/PLATO, uncertainty и target architecture |

Пороги/длительности — документированные допущения прототипа; батарея использует
one-step heuristic, не глобальную оптимизацию. Консервативная модель блокирует
неизвестную/наблюдаемую занятость до подтверждения. Нельзя интерполировать отсутствующие
счётчики или выдавать main residual за подтверждённую утечку. F5 теперь имеет два
осторожных пути: ≥3h с независимым тихим baseline и ≥4h распределённого signed-flow
без предположения, что повышенный baseline нормален. Полные парные прогоны дали
два дополнительных uncertain review flags в Dev (R403/R208), ни одного в Quickstart;
это не подтверждённые утечки или измеренный recall. [Условия и артефакты F5](docs/reviews/F5.md).

## Годовой анализ отдельно от решений

```bash
python -m engine.annual --data-dir data/PLATO40_v2_dev \
  --out insights.md --json docs/analysis_dev.json
```

[Метрики и SHA-256](docs/analysis_dev.json). Ретроспективные данные никогда не
подаются обратно в исторические решения. Import cost — наблюдаемая стоимость,
а не экономия; capped full-bin samples — не объём перелива. Нет precision/recall,
измеренных улучшений или заявлений о реальных зданиях.

## Структура и совместная работа

```text
common/             frozen схемы и gateway protocol
ingestion/         Лёша: загрузка, индексы, causal replay, guest parser
engine/            Максим: S01–S05, ресурсы, safety, mock executor, validation, annual
tests/             контракты, сценарии, counterfactual и независимые audit regressions
docs/source/       оригинальные инструкции организатора
docs/reviews/      исходный независимый review, resolution и проверенные артефакты
docs/tasks/        сохранённые исходные задания A/B
data/              локальные наборы; исключены из Git
outputs/           промежуточные результаты; исключены из Git
run.py             одна команда replay, audit и опционального анализа
```

[Контракт](common/01_CONTRACT.md), [ingestion](ingestion/README.md),
[порядок интеграции](docs/DEVELOPMENT.md). Общий контракт не меняется независимо.
Одновременные Git-операции требуют отдельных checkout/worktree; не переключать
ветку в общей рабочей папке. Старые исходные комплектные папки сохранены локально.

## Изменения после исходного `pre-holdout`

### Контрольные точки и перенос тега

Исходный annotated tag `pre-holdout` создан **13 ноября 2026, 22:48:46 CET
(Europe/Vienna)**: tag object `9a2c3288c47e5f5a826f86f59e0c3b6d4131b067`,
код `4987c7fb4e05b94e89ef6cfa894e0588b895774d`. Его исходный объект и время
сохранены без изменений в
[`pre-holdout-original-2026-11-13`](https://github.com/so-s1m4/hotel/tree/pre-holdout-original-2026-11-13).

По решению команды текущая проверенная реализация объединяется в `main`, а имя
`pre-holdout` переназначается на эту версию **с реальным временем новой операции**.
Это перенос после объявления о выпуске Holdout, а не первоначальный pre-release
checkpoint. Новый тег не датируется задним числом и не доказывает, что весь его
код существовал до 11:00. Для сравнения до/после следует использовать сохранённый
исходный тег выше; первоначальная история коммитов не переписывается.

Последняя опубликованная версия нашей линии до **14 ноября, 11:00 CET** —
[`866a448`](https://github.com/so-s1m4/hotel/commit/866a448455e5b6dce811302dc2901bb4c48f0136)
от 09:24:44. После 11:00 менялись не только документы: были исправлены входные
декодеры, поведение ingestion, сопоставление/доступность housekeeping и годовой
анализ. Все эти изменения раскрыты ниже; утверждения «core не менялся после
11:00» мы не делаем.

### Данные, использованные при доработках

**Заявление команды на момент этой фиксации:** организаторский Holdout не
расшифровывался, не распаковывался и не запускался. Его примеры, метки и результаты
не использовались для обучения, настройки правил/порогов/промптов или выбора
модели. `decisions_holdout.jsonl` ещё не создан; результата Holdout не заявляем.

Работа опиралась на открытые участникам **отдельные Quickstart и Dev**, на
синтетические regression fixtures и на независимо повторяемый source review.
Источники конкретных последующих исправлений — описанные ниже нарушения
формата/причинности/изоляции и synthetic controls, а не ошибки из Holdout.

Нельзя смешивать это заявление с утверждением «модель вообще не обучалась на
Dev»: локальный кандидат Dev100 обучался на выделенной Dev TRAIN, Synthetic100 —
на отдельной синтетической TRAIN; инструменты композиций сохраняют границы
TRAIN/VALID/TEST. Источники, checkpoint hashes и ограничения записаны в
[MODEL_CARD](docs/MODEL_CARD.md) и
[описании обучения](docs/LOCAL_LLM_TRAINING.md). Названия Dev31, parser acceptance
и диагностических наборов не обозначают организаторский Holdout. Часть
диагностик использовалась для исправлений host/parser и не выдаётся за нетронутый
финальный тест. Новое обучение или настройка по организаторскому Holdout не
выполнялись.

### Что изменилось и зачем

| Область | Причина и граница изменения | Проверяемое основание |
|---|---|---|
| Parser, время и отказы | Не выдумывать дату/UTC, не превращать отказ в заявку, не терять соседнюю независимую просьбу; keys отправляются только разрешённому origin. | Synthetic time/polarity/origin tests; [parser review](docs/reviews/LOCAL_PARSER.md), [benchmark](docs/PARSER_BENCHMARK.md). |
| Причинность движка | Учитывать уже известные события с одинаковым временем, позднюю публикацию и идентичность исправлений; не читать ещё неизвестные факты. | `test_same_time_activity_hydration`, `test_activity_hydration_adversarial`, `test_raw_publication_*`, `test_available_event_window`. |
| Локальное обучение и оценка | Проверять masking/длины/совместимость, сохранять split boundaries и отделять raw модель от host-валидации. | `tests/training/`, [training documentation](docs/LOCAL_LLM_TRAINING.md), [model card](docs/MODEL_CARD.md). Это не свидетельство 100% accuracy. |
| Сервис, Cerebras и демо | Передавать evidence настоящему DecisionEngine; queue-only routine tasks автономны, реальные исключения требуют approval/consent. Сохранить полный журнал, clock/playback, импорт новых ZIP. | `tests/bot/`, `test_guest_services`, JS playback/import tests; [demo scope](docs/DEMO_SITE.md). Site включает `enable_guest_services=True`; исходный CLI сохраняет default False. |
| BOM: validator / CLI / ZIP | Принимать допустимый BOM только в начале файла; preflight и последующее чтение не должны расходиться; не публиковать набор с непригодным JSONL. | `test_validation_bom_inputs`, `test_cli_bom_metadata`, `test_dataset_jsonl_bom`; generated output остаётся строгим UTF-8. |
| Snapshot / roster | Возвращаемый mutable event не должен менять кэш или следующий запрос; scheduled shift без publication timestamp не является подтверждённой доступной мощностью. | `test_snapshot_and_roster_bounds`; известные cutoffs и прежняя documented absence-overwrite policy сохранены. |
| On-call housekeeping | Общий human cleaning и on-call subtype должны правильно сопоставляться; неизвестный performer удерживает соответствующую capacity и отменяет несовместимые будущие предложения. | `test_oncall_observation_roles`; строгие known-ID, room/time и robot controls сохранены. Capability classes и approvals не упраздняются. |
| Годовой анализ | Эквивалентные UTC timestamps должны сортироваться и соединяться одинаково; BOM не меняет raw hashes; отсутствие coverage не выдаётся за измеренный ноль или доказанный эффект. | `test_annual_timestamp_instants`, `test_annual_bom_inputs`, `test_annual_report_coverage`. Анализ ретроспективный и не подаётся в исторические решения. |

### Полный журнал коммитов относительно исходного среза

Время ниже — **committer time в Europe/Vienna (CET)**, а не дата создания тега.
Три первых feature-коммита датированы раньше исходного тега, но в его код не
входили: они оказались в объединённой истории при merge `af73158` в 23:17.
Поэтому диапазон содержит все отсутствовавшие в исходном срезе коммиты, а не
только коммиты с более поздней календарной датой. Для перенесённых A5-патчей
показаны наши интеграционные commit IDs; оригинальные author/source histories
сохраняются в Git.

| Commit | CET | Изменение / обоснование |
|---|---|---|
| [`c58dd45`](https://github.com/so-s1m4/hotel/commit/c58dd45e97eb208b9b0039f82444aabf59bc7802) | 13.11 22:20:19 | Нидерландские относительные даты и повторная проверка непригодного кэша parser. |
| [`82be82e`](https://github.com/so-s1m4/hotel/commit/82be82e92e53288f7d26dc3983ef5ff587ea7919) | 13.11 22:21:11 | Ограниченный benchmark: раздельные raw-response и host-validation проверки. |
| [`2961124`](https://github.com/so-s1m4/hotel/commit/2961124f39b307ccf5b119866116b640d008a294) | 13.11 22:36:19 | Незавершённые измерения отмечены unknown, а не успешными нулями. |
| [`af73158`](https://github.com/so-s1m4/hotel/commit/af731582f607152485c80b6ff8d603e5483cb23d) | 13.11 23:17:40 | Интеграция main в feature-ветку; сохранена история обеих линий. |
| [`fb7c818`](https://github.com/so-s1m4/hotel/commit/fb7c818b3866226f24a7d8af63d959e37813d32b) | 13.11 23:20:46 | Опциональный constrained parsing и воспроизводимые локальные профили. |
| [`50f367b`](https://github.com/so-s1m4/hotel/commit/50f367b0cbe2b9f30747de04972c989e389160e2) | 13.11 23:21:05 | Альтернативные даты сохраняются неоднозначными вместо выдуманного выбора. |
| [`054245c`](https://github.com/so-s1m4/hotel/commit/054245cabbe0852c5bcb32b3db651f9c86bbf492) | 13.11 23:25:42 | Неоднозначность часов и проверки границ локального parser. |
| [`1647ebb`](https://github.com/so-s1m4/hotel/commit/1647ebbfadeaa85f71acc0c0bf85fe5c90047423) | 13.11 23:29:06 | Отклонение неполных clock tokens; ограничение передачи credentials нужным origin. |
| [`97ca2f0`](https://github.com/so-s1m4/hotel/commit/97ca2f09302ec7bd9ff30c6a1f6d476dac36e524) | 13.11 23:35:20 | Extraction-only профиль; вычисление UTC остаётся в проверяемом host-коде. |
| [`3c3ef90`](https://github.com/so-s1m4/hotel/commit/3c3ef902a00d2a205ba2afb6d90f0647f5f01d55) | 13.11 23:38:08 | Отдельный многоязычный acceptance-набор parser с зафиксированным составом. |
| [`8b79013`](https://github.com/so-s1m4/hotel/commit/8b7901314190dd79106e4eff188910b4183cda0f) | 13.11 23:53:17 | Явно отклонённые гостем услуги требуют review до планирования ресурсов. |
| [`efc5b45`](https://github.com/so-s1m4/hotel/commit/efc5b45224020190abb7ecdb6f3c1fac26d75355) | 13.11 23:56:59 | Проверки прямых и составных отказов с учётом области действия отрицания. |
| [`046f250`](https://github.com/so-s1m4/hotel/commit/046f2506621ce21771c1dc08bca52ce5535d5fa8) | 14.11 00:22:28 | Известные operational facts загружаются до планирования на том же timestamp. |
| [`14d1eae`](https://github.com/so-s1m4/hotel/commit/14d1eaeebf3b1555d556e386f51c6dca245585e5) | 14.11 00:37:48 | Поздно опубликованные события и идентичность объединённых исправлений. |
| [`62e51d1`](https://github.com/so-s1m4/hotel/commit/62e51d1d62c17bd7d9243a5d34bd4c5a97f6e799) | 14.11 00:57:44 | Время публикации исходного события сохраняется в normalizer и causal gateway. |
| [`b6977e8`](https://github.com/so-s1m4/hotel/commit/b6977e8b02041d95c1b98fea295320619f5f4f7e) | 14.11 01:11:59 | Документация измеренных локальных прогонов и границ integration evidence. |
| [`e8ecab1`](https://github.com/so-s1m4/hotel/commit/e8ecab1d5e4789b3f1f3945655bc5a8d6dc30759) | 14.11 01:54:24 | Подготовка/проверка локального обучения, обработка времени и release verification. |
| [`83210ef`](https://github.com/so-s1m4/hotel/commit/83210ef92fafcc1c420d4a4fdd343f89f1e3dc41) | 14.11 02:11:26 | Согласованность review-флагов при оценке нескольких намерений. |
| [`8fadd82`](https://github.com/so-s1m4/hotel/commit/8fadd82d7f9e1db44e05ef6a296fb73e5c842c0b) | 14.11 02:29:07 | Подтверждение ожидания гостя исходной фразой и разбор source time. |
| [`d658c10`](https://github.com/so-s1m4/hotel/commit/d658c10d3ce5a36e67df1065fa5a818190b94f32) | 14.11 02:30:39 | Полярность отказов; независимые просьбы не теряются при отказе от другой услуги. |
| [`de7f25f`](https://github.com/so-s1m4/hotel/commit/de7f25f98c870a9f9567d9fff7342d1d18a08c58) | 14.11 02:34:45 | Композиции только из reviewed TRAIN; VALID/TEST не переносятся в обучение. |
| [`ad2fb5d`](https://github.com/so-s1m4/hotel/commit/ad2fb5d593db39a62eb4c2665a200d1718067071) | 14.11 02:44:47 | Ошибки parser остаются reviewed; закрыты source-clock edge cases. |
| [`73cbc7c`](https://github.com/so-s1m4/hotel/commit/73cbc7ca49a9c3acba779198c1dd7a7136fd098d) | 14.11 02:53:36 | Unicode timezone offsets отклоняются без потери исходных квалификаторов. |
| [`866a448`](https://github.com/so-s1m4/hotel/commit/866a448455e5b6dce811302dc2901bb4c48f0136) | 14.11 09:24:44 | Cerebras-сервис с настоящим DecisionEngine, автономные queue-only задачи, журнал, сайт, playback и ZIP import. |
| [`2bea5d7`](https://github.com/so-s1m4/hotel/commit/2bea5d78566fc65dc48c40980a541eb4ad423cb3) | 14.11 11:23:34 | Валидатор принимает один file-leading BOM во входах; generated JSONL остаётся строгим. |
| [`42854a0`](https://github.com/so-s1m4/hotel/commit/42854a0a55df62e3ed3e6847dae117aeab431fad) | 14.11 11:35:17 | CLI читает BOM-prefixed building.json так же, как input preflight. |
| [`bb41edc`](https://github.com/so-s1m4/hotel/commit/bb41edc1859d605a0fac853b3307f6bdfe28176a) | 14.11 11:38:48 | ZIP admission применяет BOM на уровне файла, отклоняя поздние/повторные BOM. |
| [`256d5cb`](https://github.com/so-s1m4/hotel/commit/256d5cb6e158d5f1ca7004b06006b5924383ea0a) | 14.11 11:45:26 | Независимые event snapshots; смены без recorded publication не дают доступной мощности. |
| [`9d62448`](https://github.com/so-s1m4/hotel/commit/9d62448c2f51315f44e89604ffd48a97ecf83d6d) | 14.11 11:49:03 | Human/on-call observation matching, удержание неизвестной capacity и отмены с сохранением ID/room/time ограничений. |
| [`7360c7c`](https://github.com/so-s1m4/hotel/commit/7360c7ce7d9b8b8f16a9f4e714ab252824824a67) | 14.11 12:15:44 | Годовой анализ сравнивает UTC instants, а не разные текстовые записи одного времени. |
| [`8ebd2aa`](https://github.com/so-s1m4/hotel/commit/8ebd2aa6c09bc327b3554c2ffa3f3211176fe36a) | 14.11 12:15:44 | File-leading BOM поддержан в исходных JSON/JSONL/CSV годового анализа без изменения байтов. |
| [`95145e7`](https://github.com/so-s1m4/hotel/commit/95145e7b2cc2fa82fe6691fab22d36157e3d35a2) | 14.11 12:15:44 | В отчёте отсутствие coverage отделено от измеренного нуля; частичные и signed значения квалифицированы. |

Проверить диапазон можно без доступа к Holdout:

```bash
git log --reverse --format='%H %cI %s' pre-holdout-original-2026-11-13..main
git diff pre-holdout-original-2026-11-13 main -- engine ingestion run.py hotel_bot hotel_web
```

### Результаты и пределы доказательств

[Публичная сводка хешей и прогонов](docs/reviews/post_tag_verification.json)
содержит только идентичности кода/входов/выходов, команды, exit codes и агрегаты,
без ключей, исходных гостевых записей и весов модели.

На точном `866a448`: **2 362 Python tests**, **60 JS tests**; на точном
`9d62448`: **2 407 Python tests**, **60 JS tests**. Offline CLI (без `--llm`)
обрабатывает Quickstart: **3 939 событий / 342 решения**, Dev: **65 434 / 7 173**.
В обоих срезах validator возвращает 0 ошибок, пересечений ресурсов/комнат и
точных дублей уведомлений. Между этими двумя срезами decisions и audit каждого
набора побайтово совпали; все 17 входных файлов каждого набора сохранили хеши.
Это не доказывает отсутствие всех багов: synthetic edge cases исправлены, даже
если они не встречались в этих конкретных входах.

Итоговый runtime-срез `95145e7b2cc2fa82fe6691fab22d36157e3d35a2` (source tree
`c2c43183b7753e070bbd02c1220d8cfbb7f14e0f`) прошёл **2 438 Python tests** и
**60 JS tests**. Все 178 проверенных tracked code-file hashes оставались
неизменными. Полный годовой анализ исходного Dev завершился успешно: 17 входных
хешей не изменились, JSON семантически совпал с ранее опубликованным
`docs/analysis_dev.json`. Новые timestamp/BOM/coverage исправления не выдаются
за новые измеренные улучшения на этом конкретном Dev.

Настоящий README и metadata-only receipt добавлены отдельным документационным
коммитом поверх этого проверенного кода; его SHA виден в Git и не включается
в собственное содержимое. Повторный Holdout run этим не подменяется.

Site/Cerebras archives и локальные модельные эксперименты имеют отдельные
source/input/profile identities. Offline CLI результаты не выдаются за свежие
Cerebras calls или за результаты новых адаптеров. Повтор старой извлечённой
информации/кэша и Play не являются новым inference; количество свежих сетевых
вызовов нельзя выводить из счётчика обработанных сообщений.

Коммиты, synthetic tests и input hashes позволяют воспроизвести заявленные
исправления и результаты. Сами по себе Git и хеши не являются независимым
доказательством отсутствия любого внешнего доступа к данным. Проход validator
означает проверенные формат/согласованность/ограничения; incident precision,
recall, подтверждённая экономия и оценка жюри из этого не следуют. Ни `pre-holdout`,
ни этот раздел не заменяют фактический неизменённый Holdout run и его артефакты.

## Сдача и лицензии

Для сдачи ещё требуется неизменённый прогон на отдельном Holdout и реальный
`decisions_holdout.jsonl`. Исходный pre-release tag сохранён, перенос текущего
`pre-holdout` и все различия раскрыты в разделе выше. Holdout-файл пока не
расшифровывался, фиктивный результат не создаётся.
Для заявки ещё нужен реальный 3-минутный ролик и финальная демонстрация;
[сценарий записи](docs/DEMO.md) не является записанным видео.

Код — MIT. Данные — CC BY 4.0: “PLATO-40 synthetic dataset, Proximata Hackathon '26 –
Rhomberg track”. PLATO/CLARA — названия продуктов Rhomberg; PLATO-40 вымышленный.
