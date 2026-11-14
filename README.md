# Hotel Operations Prototype

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
ошибка/отсутствие модели сохраняет source-label fallback. Настройки — `.env.example`;
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

## Сдача и лицензии

После официального выпуска выполнить неизменённый pipeline на отдельном Holdout и
сохранить `decisions_holdout.jsonl`. Перед выпуском поставить `pre-holdout` на
проверенную версию. Holdout-файл не расшифровывался, фиктивный результат не создаётся.
Для заявки ещё нужен реальный 3-минутный ролик и финальная демонстрация;
[сценарий записи](docs/DEMO.md) не является записанным видео.

Код — MIT. Данные — CC BY 4.0: “PLATO-40 synthetic dataset, Proximata Hackathon '26 –
Rhomberg track”. PLATO/CLARA — названия продуктов Rhomberg; PLATO-40 вымышленный.
