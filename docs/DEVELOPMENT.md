# Параллельная разработка

## Что уже зафиксировано

Корневая `common/` побайтово скопирована из двух совпадающих исходных комплектов.
Публичные API и модели не изменены. `NormalizedEventV1` — внутренний формат команды;
`DecisionRecordV2` — выход для организатора. `available_at_utc` задаёт время replay.
`ReplayData(data_dir)` существует с точными сигнатурами gateway, но методы ещё TODO.
Предлагаемые новые функции engine в каркасе — рабочие заготовки, не frozen API.

## Лёша: feature/ingestion

1. Прочитать `docs/tasks/A_DATA_LLM.md`, `common/01_CONTRACT.md` и данные Quickstart.
2. `loader.py`: типизация пустых значений, дедупликация, индексы без повторного чтения
   полного Dev на каждом запросе; выбрать хранилище и добавить зависимость при надобности.
3. `replay.py`: все методы gateway с причинностью; тесты будущих статусов,
   смен, тарифов, прогнозов, неизвестной занятости и Europe/Vienna + DST.
4. `events.py`: стабильные source IDs, сортировка, guest/booking/roster/work-order/clock events.
5. `guest_parser.py`: source metadata отдельно от извлечения, evidence_quote из текста,
   offline fallback, опциональный LLM, кэш, несколько языков и prompt injection.
6. `ingestion.cli`: реальный поток normalized events на Quickstart и Dev без API-ключа.

Приёмка: `python -m pytest -q common/tests tests/ingestion` и экспорт событий,
включая реальный `guest:GR000005`. Сохранить существующие подписи методов.

## Максим: feature/decision-engine

1. Прочитать `docs/tasks/B_DECISION_ENGINE.md` и официальный `DECISION_CONTRACT.md`.
2. В тестах использовать `NormalizedEventV1` из `common/examples/` и тестовый gateway.
   Не импортировать конкретные внутренности loader/parsing в детекторы.
3. S01: занятость/чистота/инспекция, доступность персонала, реалистичный срок.
4. S02: дельты накопительных счётчиков, сбросы/пропуски, фон общего потребления.
5. S03: длительность холода в занятых номерах, HP1, прогнозы/тарифы по времени публикации.
6. Централизовать safety и ограничения ресурсов; executor хранит pending approval,
   не выдумывает доставку сообщений и не меняет исторические измерения.
7. Реализовать `write_decisions`, incident dedup и полную команду `run.py`.
8. `insights.md`: рекомендации CLARA/PLATO с измеренными evidence и неопределённостью.

Приёмка: `python -m pytest -q common/tests tests/engine`, затем совместный replay.

## Интеграция

Сначала контракты + unit tests каждого владельца; затем `tests/integration/` на
Quickstart. Проверить известные false positives, safety и одинаковый результат
повторного запуска. Прогнать Dev без подгонки к датам сценариев. Только затем
заполнить финальные артефакты. Тесты существующего каркаса не доказывают качество
детекции: это отдельные обязательные проверки после реализации.

В одном checkout не выполнять параллельно checkout/reset/merge/commit из разных сессий.
Используйте отдельные checkout/worktree для веток, объединяйте небольшие изменения.
Общие pyproject/uv.lock/README меняйте согласованно. После изменения зависимостей
обновлять lock: `uv lock`; проверять `uv sync --frozen --extra dev`.

## Сдача по локальному brief

- 14.11.2026 около 11:00 Europe/Vienna: ожидается holdout через Discord.
- До выпуска: проверенный commit и `git tag pre-holdout`.
- После выпуска: неизменённый pipeline на holdout и commit `decisions_holdout.jsonl`.
- 14.11.2026, 14:00 Europe/Vienna: code freeze по v2 brief. Сверить объявления организатора.
- README, MIT LICENSE, .env.example, decisions.jsonl, decisions_holdout.jsonl,
  insights.md; публичный GitHub в организации Proximata по назначенному пути,
  3-минутное end-to-end видео и письменная заявка.
- Текущий origin пользователя — рабочий репозиторий; финальный адрес в Proximata
  пока не предоставлен. Пустые submission-файлы и ранний pre-holdout не создавать.
