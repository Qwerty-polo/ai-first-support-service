# Support Draft Service

Тестове завдання AI-first Backend Engineer: сервіс готуватиме відповіді підтримки
за базою знань і надсилатиме їх лише після підтвердження оператором.

**Стан: завершено тільки етап 1 — основа.** Зараз працює `/health`, підключено
конфігурацію, SQLAlchemy, Alembic та PostgreSQL у Compose. Вебхук, worker, LLM,
інтерфейс і мок хелпдеску ще не реалізовані.

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

На цьому етапі baseline створює лише службову `alembic_version`.
Тести конкурентності з'являться разом із відповідною бізнес-логікою.

## Структура та рішення

- `app/main.py` — фабрика FastAPI, lifespan, `/health`.
- `app/config.py` — типізовані налаштування, секретний ключ через `SecretStr`.
- `app/db.py` — async engine, фабрика сесій і база моделей.
- `migrations/` — Alembic із підтримкою asyncpg.
- `docs/specification.md` — перевірені вимоги та етапи.
- `docs/decisions.md` — архітектура й межі гарантій доставки.
- `AGENTS.md` — правила та використана конфігурація агента.

Наступний етап: вебхук із транзакційним збереженням події та durable job.
Публікацію GitHub і платні зовнішні дії ще не виконували.
