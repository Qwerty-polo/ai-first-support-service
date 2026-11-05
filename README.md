# Support Draft Service

Тестове завдання AI-first Backend Engineer: сервіс готує відповіді підтримки
за базою знань і надсилає їх лише після підтвердження оператором.

**Стан: локальний сценарій реалізовано й перевірено.** Працюють `/health` і
`POST /webhooks/helpdesk`, транзакційне збереження події та generation job у PostgreSQL.
Worker підтримує захоплення, повтори, відновлення й глобальний допуск до LLM.
Worker генерує чернетки та зберігає їх у `jobs.result`. База знань доступна за `/kb/{id}`.
Оператор підтверджує або відхиляє чернетку через захищений вебінтерфейс.
Підтвердження атомарно створює delivery job з копією тексту. Worker надсилає її моку
хелпдеску зі стабільним ключем ідемпотентності. Результати перевірок — [docs/verification.md](docs/verification.md).

Архітектура, схема потоку та пояснення вибору рішень — [docs/architecture.md](docs/architecture.md).

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
- Інтерфейс: `http://localhost:8000/operator/` (або порт APP_PORT).
- Локальний вхід: `operator` / `local-demo`. Перед публічним запуском змініть пароль.
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
Для локального API/worker поза Docker задайте в `.env`
`ALLOWED_REPLY_ORIGINS=["http://127.0.0.1:8002"]` та використовуйте цю адресу в reply_url.
Worker в іншому терміналі: `uv run --locked python -m app.worker`
(також задайте `UV_PROJECT_ENVIRONMENT=.venv313`).
Змінні оточення мають пріоритет над `.env`. Не виводьте об'єкт Settings або DSN у логи.

## Перевірки

```powershell
.venv313\Scripts\python.exe -m pytest
.venv313\Scripts\ruff.exe check .
.venv313\Scripts\ruff.exe format --check .
.venv313\Scripts\python.exe -m mypy
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

Міграція `0002_events_jobs` створює `helpdesk_events` і `jobs`, а `0003_worker`
додає володіння завданнями, результати й таблиці глобального допуску до LLM.
`0004_review` додає рішення оператора й payload доставки; `0005_mock_receipts` — журнал мока.
Для інтеграційних тестів потрібен користувач PostgreSQL із правом `CREATE DATABASE`:

```powershell
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://support:support@127.0.0.1:5432/postgres'
.venv313\Scripts\python.exe -m pytest -q
```

Тести створюють випадково названу БД `support_test_<uuid>`, застосовують справжні
міграції та видаляють лише цю БД після завершення. Базу з указаного DSN не очищають.
Без `TEST_DATABASE_URL` інтеграційні тести пропускаються; це не повна перевірка.
Використовуємо `127.0.0.1`: на цій Windows IPv6 для `localhost` затримує підключення.

### Coverage та CI

```powershell
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://support:support@127.0.0.1:5432/postgres'
.venv313\Scripts\python.exe -m pytest -q --cov --cov-report=term-missing --cov-report=html --cov-report=xml
```

Інтерактивний звіт — `htmlcov/index.html`, машинний — `coverage.xml` (обидва поза Git).
Вимірюються всі модулі `app`, рядки та розгалуження. У `pyproject.toml` встановлено
поріг 90%; нижче нього команда завершується помилкою. Налаштовано greenlet concurrency
для коректного обліку SQLAlchemy async, без приховування непокритих модулів.

`.github/workflows/ci.yml` підготовлено для push/PR: locked dependencies, Ruff,
строгий mypy, міграції, тести на PostgreSQL і coverage artifact. CI не потребує
LLM-ключа; усі HTTP-виклики тестів контрольовані. Сам GitHub workflow буде виконаний
після публікації репозиторію, його віддалений запуск поки не перевірений.

Конфігурація спирається на документацію [pytest-cov](https://pytest-cov.readthedocs.io/en/latest/config.html),
[coverage concurrency](https://coverage.readthedocs.io/en/latest/config.html#run-concurrency),
[mypy](https://mypy.readthedocs.io/en/stable/config_file.html) та
[uv у GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/).

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

Для стандартного `APP_PORT=8000` замініть порт у прикладі. Мок доступний усередині
Compose як `mock-helpdesk:8000`, а з хоста — `localhost:8002`.

- Нова подія → `202 {"event_id":"evt_demo_1","status":"accepted"}` після commit.
- Повтор із тим самим вмістом → `202` зі статусом `duplicate`, без другого job.
- Той самий `event_id` з іншим вмістом → `409`, оригінал зберігається.
- Невалідні поля → `422`; недоступна/заблокована БД → `503`, `Retry-After: 1`.
- Ідентифікатори: 1–200 символів; message: 1–20 000, не лише пробіли;
  email: до 320 із базовою перевіркою структури; reply URL: HTTP(S), до 2048,
  без вбудованих credentials. Origin має бути в ALLOWED_REPLY_ORIGINS.

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
- `app/queue.py` — захоплення, ліміти, захист результату й повтори.
- `app/worker.py` — цикл worker із підмінними async-обробниками.
- `migrations/` — Alembic із підтримкою asyncpg.
- `docs/specification.md` — перевірені вимоги та етапи.
- `docs/decisions.md` — архітектура й межі гарантій доставки.
- `AGENTS.md` — правила та використана конфігурація агента.

## Worker

Compose запускає окремий процес `python -m app.worker`. Перегляд станів:

```powershell
docker compose logs worker
docker compose exec postgres psql -U support -d support -c "SELECT id, state, attempts, last_error FROM jobs;"
```

Захоплення через `FOR UPDATE SKIP LOCKED` видає випадковий `lease_token` на 60 секунд.
Обробник виконується після commit і має таймаут 40 секунд. Успіх зберігає JSON-результат
у `jobs.result` і переводить job у `succeeded` однією умовною операцією. Запис результату
чи помилки дозволений лише власнику чинного токена з непростроченою орендою.

Після падіння процесу прострочений job підхоплює інший worker. Максимум — три спроби,
включно з падіннями. Тимчасова помилка/таймаут дає повтор через 2–4 с після першої спроби,
4–8 с після другої. Постійні та неочікувані помилки одразу дають `failed`.
У `last_error` зберігається безпечний код, без тексту відповіді провайдера.

Одна заблокована на короткий час DB-стрічка `llm_policy` узгоджує допуск усіх worker.
`llm_admissions` зберігає допуски за останні 60 секунд; максимум — 60, включно з повторами.
Це бюджет допусків до обробників: адаптер має робити рівно один провайдерський запит
на допуск, без прихованих внутрішніх повторів, і починати його відразу після підготовки.
Зарезервований допуск після падіння не повертаємо — так бюджет залишається консервативним.

Окремий глобальний ліміт активних generation jobs — `llm_policy.concurrency`, типово 32.
Його можна змінити SQL-оновленням єдиного рядка `id=1` (допустимо 1–60).
Локальний паралелізм процесу — `WORKER_PARALLELISM`, типово 32; загальний ліміт БД
діє незалежно від кількості процесів. Зменшення ліміту не перериває вже активні jobs.

Обробники повинні підтримувати asyncio cancellation та власні HTTP-таймаути.
Примусова пауза ОС чи провайдер, який продовжує роботу після закриття HTTP-з'єднання,
не дають гарантії відсутності повторного зовнішнього виконання. Fencing захищає запис
у нашу БД; для доставки потрібен стабільний ключ ідемпотентності на стороні хелпдеску.

## Генерація і база знань

`app/knowledge.py` містить дев'ять коротких AI-згенерованих статей про вигаданий LumaDesk.
Пошук рахує збіг слів із заголовками та ключовими словами, відкидає службові слова
й повертає до трьох статей. Для незнайомого питання використовується стаття
`contact-support`, а результат позначається `needs_clarification=true`.
Приклад: `http://localhost:8001/kb/export-pdf` (порт залежить від APP_PORT).

За відсутності `OPENAI_API_KEY` worker робить один реальний неавторизований
`POST https://api.openai.com/v1/responses` без повідомлення клієнта. Мережевий бюджет —
3 секунди; відповідь/відмову ігноруємо, потім використовуємо статичний текст за знайденою
статтею. Загальна змодельована затримка — випадково 5–30 секунд, включно з HTTP-очікуванням.
Відмова провайдера або timeout у цьому режимі не ламають генерацію.

Із ключем використовується Responses API, `store=false`, structured JSON output.
Модель налаштовується через `OPENAI_MODEL`, типово `gpt-4.1-mini`; доступність залежить
від акаунта. Платний режим покритий MockTransport-тестами, реальні платні запити
під час розробки цього етапу не виконувалися.
Формат звірено з [OpenAI Docs](https://developers.openai.com/api/docs/guides/structured-outputs).

Адаптер виконує рівно один HTTP-запит, без прихованих повторів. 429, 5xx, мережеві
помилки й невалідні відповіді проходять через чергу повторів; 4xx (окрім 429) — постійна
помилка налаштувань/запиту. Повний timeout виклику — 30 с, timeout обробника — 40 с.

Модель повертає текст і ID статей. Ми перевіряємо ID за реально переданими джерелами,
відхиляємо дублікати та URL у тексті моделі. Посилання будуємо з наших статей і
`PUBLIC_BASE_URL` (Compose: localhost + APP_PORT). Перед деплоєм задайте публічну адресу,
доступну отримувачам відповіді. Перевірка ID гарантує існування джерел, а зміст відповіді
остаточно оцінюватиме оператор.

`DraftGenerator` — невеликий Protocol для підміни LLM у тестах, `OpenAIAdapter` — реалізація
через HTTPX, `GenerationHandler` — поєднання пошуку й генерації. Помилки інтеграцій
винесені в `app/errors.py`, щоб запуск `python -m app.worker` використовував ті самі
типи помилок, що й адаптер. Результат зберігається worker лише за чинного lease token.

## Інтерфейс оператора

Відкрийте `/` або `/operator/`; браузер запитає ім'я й пароль через HTTP Basic.
Налаштування: `OPERATOR_USERNAME`, `OPERATOR_PASSWORD`; локальні defaults —
`operator` / `local-demo`. Для публічного розгортання потрібні власний сильний пароль
і HTTPS. Облікові дані не передаються через URL чи query parameters.

Сторінка показує 25 звернень за раз: повідомлення клієнта, стан генерації, кількість
спроб, безпечний код помилки, чернетку та посилання на джерела. Кнопка Оновити отримує
поточний стан. Після рішення показується відхилення або стан delivery job.
Jinja2 екранує текст клієнта й LLM; скрипти не потрібні. Форми містять підписаний
CSRF-токен для конкретної чернетки та дії; відповіді сторінки мають `Cache-Control: no-store`.

Підтверджувати/відхиляти можна лише завершену generation job з текстом. Рішення
зберігається в `helpdesk_events.decision`. Транзакція блокує рядок звернення:
approve та створення delivery job відбуваються разом. Job отримує незмінний текст
у `payload.message`, незалежний від подальшого відображення чернетки.
Повтор тієї самої дії ідемпотентний. Спроба змінити вже прийняте рішення повертає 409.
При помилці БД дія повертає 503, рішення й delivery job відкочуються разом.

Після approve/reject сервер повертає 303 на список: оновлення сторінки не повторює POST.
Відхилене звернення не створює delivery job. Унікальність `(event_id, kind)` додатково
захищає від другої доставки. Обробник доставки додатково перевіряє approved та чинний lease.

Деталі HTTP-доставки, конфігурація та сценарії мока — у [docs/delivery.md](docs/delivery.md).

Фінальний локальний аудит завершено; проєкт підготовлено до огляду користувачем.
Публікацію GitHub і платні зовнішні дії ще не виконували.

## Демонстрація за кілька хвилин

1. Виконайте `docker compose up --build` без API-ключа.
2. Надішліть приклад вебхука вище. Зачекайте 5–30 секунд і оновіть `/operator/`.
3. Перевірте чернетку й джерела, натисніть Підтвердити, потім Оновити.
4. Відкрийте `http://localhost:8002/messages/T-1042` з обліковими даними оператора:
   має бути одна відповідь.
5. Для перевірки відхилення створіть нову подію з іншим `event_id` і ticket_id,
   натисніть Відхилити — повідомлень для цього ticket не буде.
6. Для повтору після втрати підтвердження використовуйте нову подію та
   `reply_url=.../replies?scenario=accept-timeout`: доставка завершиться після повтору,
   але `/messages/<ticket_id>` усе одно міститиме одну відповідь.

Приклад для POSIX shell (порти стандартні, без `.env`):

```bash
curl -X POST http://localhost:8000/webhooks/helpdesk \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"evt_demo_1","ticket_id":"T-1042","customer_email":"user@example.com","message":"Не можу експортувати звіт у PDF","reply_url":"http://mock-helpdesk:8000/tickets/T-1042/replies"}'
```

Проєкт використовує невеликі HTTP-адаптери з Protocol для тестування, транзакційний
журнал завдань у PostgreSQL та lease token для захисту від застарілого worker.
Відсутність дубля доставки спирається на підтримку Idempotency-Key хелпдеском;
це явне припущення, деталі — у docs/delivery.md.
