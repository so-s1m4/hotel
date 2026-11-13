# Hotel Operations Prototype

Каркас командного решения для PLATO-40: синтетического отеля на 40 номеров.
Обработка исторических данных → обнаружение риска → решение → согласование →
симуляция действия → проверка → рекомендации по эксплуатации и проектированию.

**Статус: engine S01–S03 реализован и проверен на offline scenario fixtures.**
Работают детекторы, планирование, safety gate, mock executor и официальный JSONL.
Полный replay Quickstart/Dev ожидает реализацию `ingestion.ReplayData` Лёшей.
Также доступны распаковка, аудит данных, проверка входных файлов и тесты контрактов.
Алгоритмы и ограничения описаны в [docs/ENGINE.md](docs/ENGINE.md).

## Быстрый старт

Python 3.11+; команды из корня репозитория:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
# Скопировать ZIP организатора в корень или указать --archive-dir /path/to/downloads
python scripts/prepare_data.py
python run.py --data-dir data/PLATO40_v2_quickstart --check
python -m pytest -q
```

Для воспроизводимого окружения с uv: `uv sync --frozen --extra dev`,
далее `uv run --frozen --extra dev python -m pytest -q`.
API-ключ для этих команд не нужен. Опциональные настройки будущего парсера —
в `.env.example`; автоматическая загрузка `.env` пока не реализована.

Аудит всех CSV/JSONL выбранного набора:

```bash
python -m scripts.audit_data --data-dir data/PLATO40_v2_dev --out outputs/audit_dev.json
```

## Структура и зоны ответственности

```text
common/                 исходные общие контракты v1.0, примеры, тесты
  contracts/api.py      ReplayDataGateway: исторические запросы
  contracts/schemas.py  NormalizedEventV1 → DecisionRecordV2
ingestion/             Лёша: загрузка, индексы, causal replay, LLM-парсер
engine/                Максим: детекторы S01–S03, планирование, safety, mock execution
tests/ingestion/       тесты слоя данных
tests/engine/          тесты движка на fixtures без готового ingestion
tests/integration/     общие end-to-end тесты после интеграции
scripts/               распаковка и потоковый аудит
docs/source/           оригинальные инструкции и примеры организатора
docs/tasks/            сохранённые задания для двух разработчиков
docs/audit_*.json      фактический аудит Quickstart и Dev
data/                  локальные наборы; не коммитятся
outputs/               промежуточные результаты; не коммитятся
run.py                 CLI replay + audit; --check проверяет только входы
```

Начать работу: [правила интеграции и порядок задач](docs/DEVELOPMENT.md).
Обзор материалов: [docs/PROJECT_BRIEF.md](docs/PROJECT_BRIEF.md).
Полный контракт: [common/01_CONTRACT.md](common/01_CONTRACT.md).

## Параллельная работа

- Лёша: [задание A](docs/tasks/A_DATA_LLM.md), `ingestion/`, `tests/ingestion/`.
- Максим: [задание B](docs/tasks/B_DECISION_ENGINE.md), `engine/`, `run.py`, `tests/engine/`.
- Источник истины — **одна** корневая `common/`. Не менять контракт независимо.
- Движок можно писать сейчас на `common/examples/`, не ожидая готового replay.
- Ветки: `feature/ingestion` и `feature/decision-engine`; для одновременных Git-операций
  нужны отдельные checkout/worktree. Не переключать ветки в общей рабочей папке.
- Исходные локальные пакеты `A_LESHA_DATA_LLM/` и `decision_engine_full/` сохранены,
  исключены из Git. Активную реализацию писать в корневых `ingestion/` и `engine/`.

## Команда replay

После подключения готового ingestion:

```bash
python run.py --data-dir data/PLATO40_v2_dev --out decisions.jsonl
```

**Пока gateway не реализован, без `--check` команда завершится с кодом 2,
не перезаписывая результат.** Engine не заменяет отсутствующий ingestion тестовой заглушкой.
Дополнительно: `--audit outputs/audit.jsonl`, `--insights outputs/summary.md`,
`--config engine_config.json`. Сводка решений не заменяет годовой анализ Level 3.
`decisions.jsonl`, `decisions_holdout.jsonl`, `insights.md` должны появиться после
реального запуска и анализа; образцы организатора не являются решениями нашей команды.
`pre-holdout` ставить на проверенную версию перед официальным выпуском holdout,
а не на этот каркас. Детали сдачи — в `docs/source/CHALLENGE.md`, §8–10.

## Ограничения и лицензии

Quickstart (21 день) и Dev (365 дней) — независимые симуляции; не склеивать.
Только данные, известные на момент решения; пустое значение не равно нулю;
план бронирования не доказывает присутствие, motion=0 не доказывает выезд.
Роботы не подтверждают готовность номера. Все действия — симуляция;
согласование и согласие гостя проверяются отдельно и не подразумеваются.

Код — MIT. Данные — CC BY 4.0: “PLATO-40 synthetic dataset, Proximata Hackathon '26 –
Rhomberg track”. Оригинальные документы организатора сохранены с исходной атрибуцией.
PLATO-40 вымышленный; выводы по нему не описывают реальные здания PLATO или CLARA.
