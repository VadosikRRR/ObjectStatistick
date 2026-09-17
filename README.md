# ObjectStatistick

Система мониторинга строительных объектов с разделённым Backend и GPU ML Worker.

## Архитектура

```text
RTSP camera → Backend (schedule + recording + API) → Redis queue → ML Worker (GPU)
                    │                                      │
                    └──────────── PostgreSQL ◄──────────────┘
                                      │
                            reports + Telegram notifications
```

- `backend` — FastAPI, планировщик смен, запись RTSP, журнал задач, отчёты и Telegram-уведомления.
- `worker` — единственный владелец YOLO/RT-DETR, ByteTrack и логики пересечения линий. Он масштабируется отдельно от Backend.
- `postgres` — источник истины для объектов, записей, задач, результатов и подписок.
- `redis` — только очередь длительных ML-задач, не хранилище бизнес-данных.
- Docker volume `monitoring-storage` хранит MP4 и артефакты, доступные обоим сервисам.

Backend удаляет локальные видео и артефакты только после успешной доставки отчёта и по истечении `RETENTION_DAYS` (по умолчанию 7). Строки истории в PostgreSQL при этом сохраняются.

## Структура кода

```text
src/object_statistick/
  backend/        # FastAPI, API-маршруты, планировщик, запись и отчёты
  worker/         # RQ entry point и ML-пайплайн
  integrations/   # RTSP, Redis и Telegram-адаптеры
  persistence/    # SQLAlchemy-модели и транзакции
  config/         # YAML-схемы и runtime settings
  domain/         # независимые от фреймворков ML-термины
```

## Требования и зависимости

Для запуска нужны Docker Engine, Docker Compose v2, RTSP-доступ к камерам и файлы весов моделей. Текущая временная конфигурация использует CPU-сборки PyTorch и не требует NVIDIA Container Toolkit.

Зависимости проекта описаны в [pyproject.toml](pyproject.toml), а точные версии их транзитивных зависимостей закреплены в [uv.lock](uv.lock). Docker автоматически выполняет `uv sync --locked` при сборке образа.

### Локальная разработка через uv

Установите uv одним из способов из [официальной инструкции uv](https://docs.astral.sh/uv/getting-started/installation/). Например, в Linux/macOS:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Затем uv сам создаст `.venv`, установит проект в editable-режиме и использует `uv.lock`:

```bash
uv sync --locked
```

После этого не нужны ни ручная активация виртуального окружения, ни `PYTHONPATH=src`: пакет `object_statistick` устанавливается из `src/` согласно `pyproject.toml`.

Локальный Backend и worker всё равно требуют доступных PostgreSQL, Redis, RTSP-камеры и моделей. Для запуска команд через созданное окружение используйте:

```bash
uv run --locked uvicorn object_statistick.backend.app:app --reload
uv run --locked rq worker video-processing --url redis://localhost:6379/0
```

Для учебного и обычного запуска рекомендуется Docker Compose.

### Обновление зависимостей

Изменяйте зависимости только в `pyproject.toml`, затем обновляйте lockfile:

```bash
uv lock
uv sync --locked
```

В коммит должны попадать оба файла: `pyproject.toml` и `uv.lock`. `uv.lock` обеспечивает одинаковый набор пакетов в локальной среде и Docker.

### Временный CPU-режим PyTorch

`torch` и `torchvision` привязаны к официальному CPU index PyTorch в `pyproject.toml`, поэтому `uv sync` скачивает варианты `+cpu`, а Docker worker не запрашивает GPU. Это удобно для отладки, но инференс будет заметно медленнее. Для возврата CUDA потребуется заменить `pytorch-cpu` на подходящий CUDA index и вернуть GPU reservation в `docker-compose.yml`.

## Быстрый запуск

1. Создайте конфигурацию секретов:

   ```bash
   cp .env.example .env
   ```

2. Положите веса моделей в `models/` согласно путям в [config/projects.yaml](config/projects.yaml).
3. Проверьте ROI, линию и пороги каждого объекта в `config/projects.yaml`.
4. Запустите:

   ```bash
   docker compose up --build -d
   ```

5. Проверьте Backend: `curl http://localhost:8000/health`.

## Переменные окружения

Создайте `.env` из [.env.example](.env.example). В Docker Compose пользователю нужно заполнить следующие переменные:

| Переменная | Назначение | Обязательность |
| --- | --- | --- |
| `VIDEO_SOURCE_PHILHARMONIC` | RTSP URL камеры «Филармония». | Да, пока объект включён. |
| `VIDEO_SOURCE_MINSTROY` | RTSP URL камеры «Минстрой». | Да, пока объект включён. |
| `TELEGRAM_BOT_TOKEN_PHILHARMONIC` | Токен Telegram-бота для «Филармонии». | Нет, если Telegram-уведомления не нужны. |
| `TELEGRAM_BOT_TOKEN_MINSTROY` | Токен Telegram-бота для «Минстроя». | Нет, если Telegram-уведомления не нужны. |
| `LOG_LEVEL` | Уровень Python-логов, например `INFO` или `DEBUG`. | Нет, значение по умолчанию — `INFO`. |
| `RETENTION_DAYS` | Сколько дней хранить локальные записи и артефакты после доставки отчёта. | Нет, значение по умолчанию — `7`. |

Для нового объекта добавляются новые пары, например `VIDEO_SOURCE_OBJECT_3` и `TELEGRAM_BOT_TOKEN_OBJECT_3`; их имена указываются в YAML, а не сами секреты.

`DATABASE_URL`, `REDIS_URL`, `PROJECTS_CONFIG` и `STORAGE_ROOT` задаются Compose внутри контейнеров. Их не нужно добавлять в `.env` при стандартном Docker-запуске. При локальном запуске они могут быть переопределены переменными окружения.

## Конфигурация объектов

[config/projects.yaml](config/projects.yaml) — единственный реестр объектов. Backend валидирует его до старта через Pydantic. Одна секция `projects` описывает один строительный объект:

| Поле | Значение |
| --- | --- |
| `id` | Постоянный технический идентификатор из строчных букв, цифр, `_` и `-`. Используется в БД и путях файлов. |
| `name` | Отображаемое имя объекта в отчётах. |
| `timezone` | Часовой пояс расписания, например `Asia/Yekaterinburg`. |
| `camera_source_env` | Имя RTSP-переменной из `.env`, а не сам URL. |
| `telegram_bot_token_env` | Имя переменной с Telegram-токеном; поле можно не задавать. |
| `shifts` | Номер, начало и конец смены. Смена не может пересекать полночь. |
| `roi` | `[x1, y1, x2, y2]` — область анализа в координатах исходного кадра. |
| `line` | Две точки линии подсчёта **в локальных координатах ROI**. |
| `line_in_is_entry` | Если `true`, направление `LineZone in` — вход на объект; иначе входом считается `out`. |
| `frame_skip` | Анализируется каждый N-й кадр. `2` сохраняет текущую настройку и примерно вдвое снижает нагрузку. |
| `tracking` | Параметры ByteTrack: `track_activation_threshold`, `lost_track_buffer`, `minimum_matching_threshold`, `frame_rate`. Значения по умолчанию: `0.25`, `90`, `0.85`, `10`. |
| `model.path` | Путь к весам внутри ML worker. В Compose каталог `./models` смонтирован в `/app/models`. |
| `model.thresholds` | Confidence-пороги от `0` до `1` для `Person`, `Technik` и `Car`. |

Если `frame_skip` или блок `tracking` не указаны, код использует приведённые значения по умолчанию. Для воспроизводимости рекомендуется явно оставлять их в YAML, как в текущих двух объектах.

### Новый объект

1. Добавьте блок в `projects.yaml` с уникальным `id`.
2. Добавьте RTSP URL и, при необходимости, Telegram-токен в `.env`.
3. Положите веса в `models/` и укажите их контейнерный путь в `model.path`.
4. Настройте ROI, локальную линию, направление пересечения, пропуск кадров, трекер и пороги классов.
5. Перезапустите `backend` и `worker`: `docker compose up -d --build`.

Секреты не хранятся в YAML: в нём указываются только имена env-переменных (`camera_source_env`, `telegram_bot_token_env`).

## API

- `GET /health` — готовность Backend;
- `GET /projects` — безопасная версия загруженной конфигурации;
- `GET /recordings` — история RTSP-записей и их статусы;
- `GET /jobs` — история ML-задач и результаты;
- `POST /jobs/{job_id}/retry` — повторно ставит неуспешную задачу в очередь;
- `POST /projects/{project_id}/subscribers` с `{"chat_id": 123}` — административно подписывает Telegram-чат на отчёты объекта.

Telegram-бот автоматически подписывает чат после команды `/start` или любого сообщения; API остаётся удобным административным способом. Отчёт создаётся только когда успешно завершены все смены дня конкретного объекта. Excel и почасовой график персонала отправляются в Telegram как вложения.

## Состояния

```text
recording → recorded → queued → processing → completed → reported
                                   └──────→ failed
```

Задачи и результаты переживают перезапуск контейнеров. Worker автоматически повторяет неуспешную задачу три раза с задержкой, а затем её можно безопасно поставить в очередь через API.

## Ограничения учебной версии

- Смены не пересекают полночь: такую смену нужно описать двумя интервалами.
- Один ML worker должен обслуживать одну GPU; несколько worker-ов запускаются только при достаточном числе GPU.
- Telegram long-polling выполняется как адаптер Backend и не требует отдельного сервиса; при появлении других каналов доставки его можно вынести без изменения расчёта отчётов.
