# Support Draft Service

Тестове завдання AI-first Backend Engineer: сервіс готуватиме відповіді підтримки
за базою знань і надсилатиме їх лише після підтвердження оператором.

**Стан: завершено етап 2 — приймання вебхука.** Працюють `/health` і
`POST /webhooks/helpdesk`, транзакційне збереження події та generation job у PostgreSQL.
Worker, LLM, інтерфейс і мок хелпдеску ще не реалізовані; jobs залишаються `pending`.

## Docker Compose

Потрібен Docker Desktop із запущеним Linux engine та Docker Compose v2.

```powershell
docker compose up --build
```

`docker compose up` також працює і будує образ, якщо його ще немає.
Compose запускає PostgreSQL, чекає його готовності, виконує `alembic upgrade head`
в одноразовому контейнері `migrate`, потім запускає API.
Зовнішні ключі та файл `.env` для цього етапу не потрібні.

- Перевірка: `http://localhost:8000/health` → HTTP 200, `{"status":"ok"}`.
- OpenAPI: `http://localhost:8000/docs`.
- БД: localhost:5432, database/user/password: `support` (лише локальне демо).
- Порти прив'язані до localhost; контейнер API працює без root.

```powershell
Invoke-RestMethod http://localhost:8000/health
docker compose ps -a
docker compose logs migrate app
docker compose stop
```

Дані PostgreSQL живуть у named volume. `docker compose down` зберігає їх;
`docker compose down -v` **видаляє всі дані** й потрібен лише для свідомого скидання.
Якщо порт 8000 зайнятий, задайте `APP_PORT=8001` у `.env` та використовуйте
`http://localhost:8001/health`. Для зайнятого 5432 змініть port mapping у `compose.yaml`
і локальний `DATABASE_URL` відповідно (внутрішня адреса Compose лишається незмінною).

## Локальна розробка на Windows

Потрібні uv 0.12.10 та Python 3.13.15 (uv може завантажити його автоматично).
Точні runtime/dev-залежності й hashes збережені в `uv.lock`.
Стару `.venv` на Python 3.14 не використовуємо й не видаляємо.

```powershell
$env:UV_PROJECT_ENVIRONMENT = '.venv313'
uv sync --locked
Copy-Item .env.example .env
docker compose up -d postgres
uv run --locked alembic upgrade head
uv run --locked uvicorn app.main:app --reload --no-access-log
```

Якщо `.env` уже існує, збережіть його власні значення замість перезапису.
Для `/health` БД не потрібна: можна одразу запустити останню команду.
Змінні оточення мають пріоритет над `.env`. Не виводьте об'єкт Settings або DSN у логи.

## Перевірки

```powershell
.venv313\Scripts\python.exe -m pytest
.venv313\Scripts\ruff.exe check .
.venv313\Scripts\ruff.exe format --check .
.venv313\Scripts\alembic.exe heads
docker compose config --quiet
```

Тест `/health` використовує недоступну адресу БД та реальний lifespan FastAPI:
перевіряє, що liveness не потребує з'єднання з PostgreSQL.
Для перевірки міграції потрібен справжній PostgreSQL:

```powershell
docker compose run --rm migrate
docker compose run --rm migrate alembic current
```

Поточна міграція `0002_events_jobs` створює `helpdesk_events` і `jobs`.
Для інтеграційних тестів потрібен користувач PostgreSQL із правом `CREATE DATABASE`:

```powershell
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://support:support@127.0.0.1:5432/postgres'
.venv313\Scripts\python.exe -m pytest -q
```

Тести створюють випадково названу БД `support_test_<uuid>`, застосовують справжні
міграції та видаляють лише цю БД після завершення. Базу з указаного DSN не очищають.
Без `TEST_DATABASE_URL` інтеграційні тести пропускаються; це не повна перевірка.
Використовуємо `127.0.0.1`: на цій Windows IPv6 для `localhost` затримує підключення.

## Вебхук

```powershell
$payload = @{
    event_id = 'evt_demo_1'
    ticket_id = 'T-1042'
    customer_email = 'user@example.com'
    message = 'Не можу експортувати звіт у PDF'
    reply_url = 'http://mock-helpdesk:8000/tickets/T-1042/replies'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8001/webhooks/helpdesk `
    -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($payload))
```

Для стандартного `APP_PORT=8000` замініть порт у прикладі. Мок ще не існує,
але на цьому етапі API лише зберігає `reply_url` і не звертається до нього.

- Нова подія → `202 {"event_id":"evt_demo_1","status":"accepted"}` після commit.
- Повтор із тим самим вмістом → `202` зі статусом `duplicate`, без другого job.
- Той самий `event_id` з іншим вмістом → `409`, оригінал зберігається.
- Невалідні поля → `422`; недоступна/заблокована БД → `503`, `Retry-After: 1`.
- Ідентифікатори: 1–200 символів; message: 1–20 000, не лише пробіли;
  email: до 320 із базовою перевіркою структури; reply URL: HTTP(S), до 2048,
  без вбудованих credentials. Allowlist адрес додамо перед зовнішніми доставками.

Унікальний primary key `event_id` і `INSERT ... ON CONFLICT DO NOTHING` захищають
від гонки. Подія й завдання комітяться однією транзакцією; при помилці job подія
також відкочується. Вебхук не викликає LLM і не надсилає повідомлень клієнтам.
Підхід до конкурентного INSERT описано в [документації SQLAlchemy](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert).

Бюджет обробки — 1.5 секунди, таймаути з'єднання й SQL-команди — 1 секунда,
очікування пулу — 0.3 секунди. Це залишає запас до таймауту хелпдеску за нормальних
умов; мережа/планування ОС можуть впливати на повний HTTP-час. Якщо commit відбувся,
але HTTP-відповідь загубилася, повтор знайде вже збережений `event_id`.

## Структура та рішення

- `app/main.py` — фабрика FastAPI, lifespan, `/health`.
- `app/config.py` — типізовані налаштування, секретний ключ через `SecretStr`.
- `app/db.py` — async engine, фабрика сесій і база моделей.
- `app/models.py` — події та durable jobs.
- `app/webhooks.py` — валідація й атомарне приймання події.
- `migrations/` — Alembic із підтримкою asyncpg.
- `docs/specification.md` — перевірені вимоги та етапи.
- `docs/decisions.md` — архітектура й межі гарантій доставки.
- `AGENTS.md` — правила та використана конфігурація агента.

Наступний етап: worker, leases, відновлення, повтори та спільний ліміт LLM.
Публікацію GitHub і платні зовнішні дії ще не виконували.
