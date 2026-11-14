# Parser: Cerebras API и локальная модель

## Cerebras API без дообучения

Для облачного запуска можно выбрать `gpt-oss-120b` на Cerebras. Модель получает
задачу извлечения намерений и дословных цитат; она не отвечает гостю и не выполняет
действия отеля. Например, вопрос о пароле Wi-Fi должен стать `wifi_help`, а не
сгенерированным паролем. Доступность модели проверяется для конкретного ключа;
само подключение API не доказывает качество на Quickstart или Dev.

Ключ храните вне репозитория, например в `$HOME/.config/hotel/cerebras.key`:
права файла `600`, каталога `700`. Не вставляйте его в команды, отчёты или общий
чат. Пример ниже читает уже созданный файл без вывода его содержимого. Функция
parser читает окружение процесса; полный ingestion/replay с включённой моделью
сначала загружает отсутствующие переменные из `.env` текущего каталога.
Хранилища ключей parser самостоятельно не открывает.

Запускайте блок без shell tracing (`set -x`). Подоболочка ограничивает настройки
этим запуском. Конкурирующие provider aliases заданы пустыми явно, чтобы loader
не восстановил их из старого `.env`:

```bash
(
  export LLM_API_KEY='' LLM_MODEL='' OPENROUTER_API_KEY='' OPENAI_API_KEY=''
  export CEREBRAS_BASE_URL='https://api.cerebras.ai/v1'
  export CEREBRAS_API_KEY="$(cat "$HOME/.config/hotel/cerebras.key")"
  export CEREBRAS_MODEL='gpt-oss-120b'
  export LLM_BASE_URL='https://api.cerebras.ai/v1'
  export LLM_RESPONSE_FORMAT='json_schema'
  export LLM_GENERATION_PROFILE='extraction'
  export LLM_REASONING_EFFORT='low'
  export LLM_MAX_TOKENS='4096'
  export LLM_TIMEOUT_SECONDS='60'
  export GUEST_PARSER_WORKERS='1'
  export GUEST_PARSER_OFFLINE='0'
  export GUEST_PARSER_CACHE_DIR='.cache/guest_parser/cerebras'

  python run.py --data-dir data/PLATO40_v2_quickstart --llm \
    --out outputs/cerebras_quickstart.jsonl \
    --audit outputs/cerebras_quickstart_audit.jsonl
)
```

Это облачный запуск: provider получает весь переданный parser объект
`guest_record` (текст, исходные поля и `request_type`, если он есть), а также
отфильтрованный исторический контекст — время сообщения, плановые даты брони
и допустимые ID комнат, когда они переданы. Полный архив, телеметрия и будущие
результаты в запрос не включаются. Для проверки именно понимания текста
`request_type` необходимо отдельно убрать из входных записей, сохранив gold
для локального сравнения. Не выдавайте результаты с подсказанной меткой за
независимую точность классификации.

Для новых сообщений без заранее размеченного типа доверенный сервер может
передать `context['source_label_policy']='optional'` и не передавать
`request_type` (либо задать null). Это снимает только механическое требование
исходной метки; неопределённое время, неверные цитаты, инструкции внутри текста,
отрицания и ошибки API по-прежнему требуют review. Пустая строка, неверная
или противоречащая ответу модели исходная метка также остаются основанием
для review. Политика входит в cache identity. Пользовательский текст и поля
сообщения не могут включить её; обычный исторический replay сохраняет
прежнюю политику. Это разрешение на классификацию, а не на действия отеля.

Для Cerebras `LLM_MAX_TOKENS` передаётся как `max_completion_tokens`: лимит
включает reasoning и конечный JSON. `4096` — начальная настройка, не гарантия
скорости или достаточности на любом сообщении. `LLM_REASONING_EFFORT=low`
задаёт ограниченный уровень рассуждения. Provider-совместимая строгая схема
задаёт форму JSON; проверки цитат, времени, ограничений и согласований остаются
в коде. Ошибка API, rate limit или усечение ответа дают явный fallback с review,
а не успешную модельную классификацию. Повторный запуск может использовать
валидный cache; для свежего измерения нужен новый пустой каталог cache.

Перед сменой принятой модели сравните свежие ответы на одинаковых входах,
отдельно учитывая семантические ошибки, fallback, время и стоимость. Начинайте
с одного worker; повышайте параллельность только после проверки лимитов аккаунта.
API не меняет правило явного `--llm`: обычный запуск CLI остаётся offline.

## Локальный профиль

Parser принимает OpenAI-compatible `/v1/chat/completions`. Для локального
Ollama-профиля используем `http://127.0.0.1:11434/v1`, один worker,
`json_schema`, максимум 512 выходных токенов и timeout 15 секунд.
Для этой машины проверяется `ministral-3:3b` с профилем `extraction`:
модель извлекает намерения и цитаты, а host вычисляет время. Результаты
сравнения с 8B и ограничения — в [отчёте](reviews/LOCAL_PARSER.md).

## Сервер

На этой машине Ollama 0.34.1 и обе модели установлены вне репозитория.
Текущий сервер слушает только loopback. После остановки его можно запустить
с теми же настройками; если Ollama установлен обычным способом, замените
путь к исполняемому файлу на `ollama`:

```bash
OLLAMA_HOST=127.0.0.1:11434 \
OLLAMA_MODELS="$HOME/.local/share/hotel-llm/models" \
OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 \
OLLAMA_CONTEXT_LENGTH=4096 OLLAMA_NO_CLOUD=1 \
  "$HOME/.local/share/hotel-llm/ollama-0.34.1/ollama" serve
```

Полный формат cohort, правила измерения и отчётов:
[бенчмарк parser](PARSER_BENCHMARK.md). Включённые примеры — синтетические
направленные проверки, не репрезентативная выборка гостей.

## Конфигурация приложения

```bash
export LLM_BASE_URL='http://127.0.0.1:11434/v1'
export LLM_API_KEY='ollama'
export LLM_MODEL='ministral-3:3b'
export LLM_RESPONSE_FORMAT='json_schema'
export LLM_GENERATION_PROFILE='extraction'
export LLM_MAX_TOKENS='512'
export LLM_TIMEOUT_SECONDS='15'
export GUEST_PARSER_WORKERS='1'
export GUEST_PARSER_OFFLINE='0'

python run.py --data-dir data/PLATO40_v2_quickstart --llm \
  --out outputs/local_quickstart.jsonl --audit outputs/local_quickstart_audit.jsonl
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
`extraction` требует UTC=null и details={} в ответе модели; host сохраняет
дословные фразы и сам разрешает обоснованные даты. Полный профиль `contract`
остаётся значением по умолчанию. Пустые details не доказывают отсутствие
пожеланий гостя: структурированные assistance_requested здесь не извлекаются.
Явный отказ от поддерживаемой услуги ставит review и убирает requested UTC,
даже если модель выбрала положительный intent. Это ограниченный набор правил,
а не полное понимание любого отрицания или сарказма.
Авторизованные запросы не следуют HTTP redirects. Provider alias может передать
свой ключ только при совпадении scheme, hostname и effective port; credentials
в самом URL отклоняются до обращения к серверу.
Base URL также не допускает query/fragment, включая пустые `?` и `#`.

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
export GUEST_PARSER_OFFLINE='0'

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
  --generation-profile extraction
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

Фактические модельные прогоны и полный Quickstart фиксируются отдельно в
[отчёте](reviews/LOCAL_PARSER.md), с конкретными версиями и границами проверки.
Прохождение schema или синтетических tests не подтверждает отсутствие ошибок
в понимании гостя и не разрешает реальные действия отеля.
