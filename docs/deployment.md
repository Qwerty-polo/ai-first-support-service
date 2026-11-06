# Тимчасове демо на EC2

Окремий Compose project `support-assignment-demo` має власну мережу й PostgreSQL volume.
На хості API слухає лише `127.0.0.1:18081`, mock — `127.0.0.1:18082`, PostgreSQL
не публікує порт. Нові контейнери мають обмеження RAM/CPU для співіснування з іншим проєктом.
Це демонстраційне розгортання, а не конфігурація високої доступності.

Поточне тимчасове демо: `https://support-demo-13-49-224-87.sslip.io`.
Працює на EC2 в окремому каталозі `/home/ubuntu/ai-first-support-service-demo`.
Унікальні секрети лишаються в ігнорованому `.env.demo`; облікові дані надаються окремо.

## Налаштування

Створіть `.env.demo` з правами 600, унікальними випадковими секретами та значеннями:

```dotenv
DEMO_DB_PASSWORD=<random-hex-secret>
OPERATOR_USERNAME=operator
OPERATOR_PASSWORD=<unique-random-secret>
HELPDESK_API_TOKEN=<unique-random-secret>
OPENAI_API_KEY=
PUBLIC_BASE_URL=https://<demo-host>
```

Файл і реальні секрети не комітяться. `DEMO_DB_PASSWORD` має бути URL-safe, наприклад hex.
Відсутні обов'язкові значення зупиняють Compose до запуску.

```bash
docker compose --env-file .env.demo -f compose.yaml -f compose.demo.yaml config --quiet
docker compose --env-file .env.demo -f compose.yaml -f compose.demo.yaml build app
docker compose --env-file .env.demo -f compose.yaml -f compose.demo.yaml up -d --no-build
```

Збираємо один спільний образ для API, worker, migrate і mock. Для обмеженого сервера
не запускаємо тестову БД або масові тести поруч із робочим застосунком.

## HTTPS через наявний Caddy

Окремий site block, після перевірки поточної конфігурації й резервної копії:

```caddyfile
<demo-host> {
    handle_path /mock/* {
        reverse_proxy 127.0.0.1:18082
    }
    handle {
        reverse_proxy 127.0.0.1:18081
    }
}
```

Спочатку `caddy validate`, потім `caddy reload` або штатний `systemctl reload caddy`.
Не зупиняти Caddy і не змінювати site blocks іншого проєкту.
Перевірити і новий hostname, і доступність та ідентичність контейнерів наявного проєкту.

Для тимчасової адреси можна використати DNS із вбудованою IP через
[sslip.io](https://sslip.io/); це залежність від стороннього DNS-сервісу.
[Caddy](https://caddyserver.com/docs/automatic-https) отримує публічний сертифікат
за умови коректного DNS і доступних 80/443.

Reply URL у webhook лишається внутрішнім:
`http://mock-helpdesk:8000/tickets/T-DEMO/replies`.
Отримані відповіді доступні оператору за `/mock/messages/T-DEMO` з тим самим HTTP Basic.
`/health` — публічний, `/operator/` вимагає унікальний пароль.

## Завершення демонстрації

Лише після погодження користувача: прибрати окремий Caddy site block із validate/reload,
потім виконати `docker compose --env-file .env.demo -f compose.yaml -f compose.demo.yaml down`
у каталозі цього демо. Без `-v`: дані зберігаються до окремого рішення про видалення.
Не зупиняти весь EC2 та не виконувати глобальні `docker prune` або `docker stop`.
