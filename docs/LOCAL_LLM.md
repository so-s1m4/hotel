# Локальный parser: ограниченный запуск

Parser принимает OpenAI-compatible `/v1/chat/completions`. Для локального
Ollama-профиля используем `http://127.0.0.1:11434/v1`, один worker,
`json_schema`, максимум 512 выходных токенов и timeout 15 секунд.
Это профиль для проверки подключения и формата, а не подтверждённый выбор
лучшей модели или измерение её качества. Сервер и модель оператор запускает
отдельно; команды ниже ничего не устанавливают и не скачивают.

Полный формат cohort, правила измерения и отчётов:
[бенчмарк parser](PARSER_BENCHMARK.md). Включённые примеры — синтетические
направленные проверки, не репрезентативная выборка гостей.

## Конфигурация приложения

```bash
export LLM_BASE_URL='http://127.0.0.1:11434/v1'
export LLM_API_KEY='ollama'
export LLM_MODEL='REPLACE_WITH_INSTALLED_MODEL_NAME'
export LLM_RESPONSE_FORMAT='json_schema'
export LLM_MAX_TOKENS='512'
export LLM_TIMEOUT_SECONDS='15'
```

`ollama` здесь — явное несекретное значение для локального сервера без
аутентификации. Оно не заменяет реальный ключ защищённого сервера.
Не используйте credentials в URL. Не переносите настоящие provider keys
в профиль локального сервера.

По умолчанию parser сохраняет `json_object`; `json_schema` включается явно.
Допустимый cap — целое число 1..16384; отсутствие cap не задаёт выходной
лимит серверу. Unsupported schema, ошибка транспорта, невалидный ответ или
явный `finish_reason`, отличный от `stop`, дают fallback с review.
Ответ без `finish_reason` поддерживается для совместимости backend.
Schema не отменяет проверку evidence, времени и остальных ограничений на host.
Авторизованные запросы не следуют HTTP redirects. Provider alias может передать
свой ключ только при совпадении scheme, hostname и effective port; credentials
в самом URL отклоняются до обращения к серверу.

## Отдельный план и выполнение

Замените имя модели и полный digest реально установленной ревизии. Сам
аргумент `--revision` не проверяет загруженные сервером веса. Перед запуском
запишите подтверждённые настройки runtime в отдельный JSON: runtime/version,
model digest, quantization, context, server parallel slots, maximum loaded
models и machine/RAM, когда эти сведения доступны. Неизвестные поля оставьте
отсутствующими; значения оператора не становятся измерениями.

```bash
candidate_model='REPLACE_WITH_INSTALLED_MODEL_NAME'
candidate_revision='sha256:REPLACE_WITH_THE_FULL_64_HEX_DIGEST'
export LLM_API_KEY='ollama'

benchmark_args=(
  --input tests/fixtures/parser_benchmark_cases.jsonl
  --limit 3
  --candidate local-schema-pilot
  --revision "$candidate_revision"
  --cache-root outputs/local-parser/cache
  --base-url http://127.0.0.1:11434/v1
  --model "$candidate_model"
  --workers 1
  --timeout-seconds 15
  --response-format-mode json_schema
  --max-tokens 512
  --runtime-metadata outputs/local-parser/runtime-cold.json
)

python -m scripts.benchmark_guest_parser "${benchmark_args[@]}" \
  --run-id cold-01 --out outputs/local-parser/plan-cold-01.json

python -m scripts.benchmark_guest_parser "${benchmark_args[@]}" \
  --run-id cold-01 --out outputs/local-parser/cold-01.json --execute
```

Файл `runtime-cold.json` подготовьте до plan и execute по формату
[бенчмарка](PARSER_BENCHMARK.md#structured-generation-and-declared-runtime-settings).
В `load_state` явно укажите, была ли модель загружена до первого запроса.
Если этого не проверяли, напишите, что состояние загрузки неизвестно.
Сохраните plan и отдельный execute report; существующие выходные файлы
перезаписывать нельзя. Три случая проверяют ограниченный профиль, а не всё
покрытие. Для расширения cohort задайте новый run ID и явный limit.

## Холодный запуск, прогрев и cache

Первую загрузку весов нельзя незаметно исключать из времени. Если перед
измерением выполнялся отдельный прогрев, запишите его время, число запросов,
модель и результат; затем используйте новый run ID, пустой parser cache и
`load_state`, соответствующий проверенному состоянию сервера. Холодный и
прогретый запуск сохраняйте отдельными отчётами. Укажите также фактическую
серверную параллельность: один client worker не доказывает число server slots.

`--warm` означает повторную проверку **parser cache** с совпадающей identity.
Это не новый прогретый inference и не измерение скорости модели. Изменение
format, generation schema, cap или объявленных runtime settings требует новой
identity. Успешные ответы кэшируются атомарно; ошибки/fallback не кэшируются.

Начальный профиль использует один worker. Шесть одновременных клиентских
запросов могут ждать очередь одного локального сервера; HTTP timeout не
разделяет ожидание очереди, загрузку и генерацию. Увеличение timeout или числа
workers — отдельное явно записанное сравнение, а не повод скрывать fallback.

## Что уже проверено

Транспортные integration tests подменяют HTTP и блокируют реальные sockets.
Они проверяют opt-in schema, cap/format identity cache, отказ от усечённых
ответов, ограничение размера HTTP body, отсутствие чувствительных строк в
error logs и конкурентный cache write при независимых timeout.

Фактический локальный pilot выполняется оператором отдельно. Здесь нет
оценок качества, времени inference или расхода памяти для реальной модели.
Прохождение schema или синтетических tests не подтверждает отсутствие ошибок
в понимании гостя и не разрешает реальные действия отеля.
