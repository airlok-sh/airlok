---
name: airlok-yml
description: >-
  Подготовка проекта к выкатке на airlok: написать или обновить airlok.yml — стек, команды, база, загрузки, задачи по
  расписанию и полный список переменных окружения с описаниями, чтобы выкатка не упала на старте. Use when the user asks
  to prepare the project for airlok, write or fix airlok.yml, list the env vars the project needs, or before the first
  deploy of a project that has no airlok.yml.
---

# airlok.yml: подготовить проект к выкатке

`airlok.yml` лежит в корне проекта (в монорепозитории — в папке приложения) и говорит airlok, что проекту нужно. Стек и
команды airlok определяет сам, поэтому главное в файле — **переменные окружения**: каждая, которую читает код, с
понятным человеку описанием. По этому списку дашборд до выкатки показывает, что заполнить, а выкатка без обязательных
останавливается сразу, а не падает через минуты сборки.

## Порядок

1. **Пойми, что это за проект.** Next.js (`next` в package.json), Node (Express, Nest, Fastify… или `scripts.start`),
   Python (requirements.txt, pyproject.toml, Pipfile), Go (`go.mod`), статика (Vite, Astro… или `index.html`), бот или
   воркер без сайта. Если в репозитории несколько приложений — у каждого свой `airlok.yml` в его папке.
2. **Найди все переменные, которые читает код** — не только из `.env.example`:
   - JS/TS: `process.env.X`, `process.env['X']`, деструктуризация `const { X } = process.env`, `import.meta.env.VITE_X`,
     `NEXT_PUBLIC_X`, схемы zod/envalid/t3-env;
   - Python: `os.environ['X']`, `os.environ.get('X')`, `os.getenv('X')`, поля `pydantic` `BaseSettings`, `environ.Env`;
   - Go: `os.Getenv("X")`, `os.LookupEnv("X")`, теги `env:"X"` (envconfig, caarlos0/env), `viper.BindEnv`/`AutomaticEnv`;
   - а ещё `.env.example`, `.env.sample`, `compose.yml` (`${X}`), README и конфиги библиотек (Sentry, S3, SMTP).
3. **Для каждой реши:**
   - `required` — без неё приложение не стартует или ломается главная функция (код бросает ошибку, нет значения по
     умолчанию). Иначе `required: false` или `default`;
   - `secret: true` — ключи, токены, пароли, строки подключения: дашборд предложит сгенерировать случайное значение;
   - `default` — значение, с которым код работает без переменной (возьми из кода, не придумывай);
   - `build: true` — значение нужно **сборке**, а не только запуску: читается в `next.config.js`, `vite.config.ts`,
     скриптах сборки (загрузка source maps в Sentry, генерация страниц из API). `NEXT_PUBLIC_*` и `VITE_*` идут в
     сборку сами — фреймворк встраивает их в код, отмечать не нужно. Секреты отмечай, только если без них сборка не
     пройдёт: сборка запускает скрипты чужих пакетов;
   - `description` — по-русски, для человека без опыта: что это и **где взять**. «Токен бота — выдаёт @BotFather»,
     «Ключ API из кабинета OpenAI → API keys», «Любая длинная случайная строка: openssl rand -hex 24».
4. **Не вписывай то, что задаёт airlok**: `PORT`, `HOST`, `HOSTNAME`, `HOME`, `PATH`, `NODE_ENV`, `DATABASE_URL`,
   `UPLOAD_DIR`, `UPLOAD_MAX_BYTES`, `AIRLOK_*`, `DOCKER_*`. Базу просят через `capabilities.database`, а не переменной.
5. **Возможности — по коду:**
   - Postgres (pg, prisma, drizzle, psycopg, asyncpg, SQLAlchemy, Django, pgx, gorm) → `capabilities.database.enabled:
     true`, код берёт адрес из `DATABASE_URL`. SQLite или файлы рядом с кодом не переживут перезапуск — предложи
     перейти на базу;
   - пользователи загружают файлы → `capabilities.uploads` с явным списком расширений, код пишет в `UPLOAD_DIR`;
   - задачи по расписанию (node-cron, APScheduler, celery beat) → `capabilities.cron` (только на хостинге airlok; на
     своём сервере пока нет);
   - миграции (`prisma migrate deploy`, `alembic upgrade head`, `python manage.py migrate`, `goose up`) →
     `run.release`: выполняется один раз перед каждой новой версией.
6. **Проверь запуск**: веб-приложение слушает `0.0.0.0` и порт из `PORT`. Захардкоженный порт или `localhost` —
   исправь в коде, это частая причина «сайт не отвечает».
7. **Запиши файл** и, если есть `.env.example`, дополни его теми же именами без значений — с комментарием-описанием.

## Формат

```yaml
version: 1
app:
  type: web                  # worker — бот или воркер без сайта
  stack: node                # static, nextjs, node, python, go (docker — только на хостинге) — если определилось не то
build:
  command: npm run build     # одна строка до 500 символов
  output: dist               # только для статики: каталог готового сайта
run:
  command: node dist/server.js
  release: npx prisma migrate deploy
runtime:
  node: "22"                 # 20, 22, 24; python: 3.10–3.14; go: 1.25–1.27
capabilities:
  database:
    enabled: true
  uploads:
    enabled: true
    allowed: [jpg, png, webp, pdf]
    max_size_mb: 10
env:
  TELEGRAM_BOT_TOKEN:
    secret: true
    description: Токен бота — выдаёт @BotFather командой /newbot
  OPENAI_API_KEY:
    secret: true
    description: Ключ API из кабинета OpenAI → API keys
  NEXT_PUBLIC_SITE_URL: Адрес сайта для ссылок и мета-тегов, например https://shop.ru   # попадёт и в сборку
  SENTRY_AUTH_TOKEN:
    secret: true
    build: true
    required: false
    description: Токен Sentry для загрузки source maps при сборке
  ADMIN_CHAT_ID: ID чата администратора — узнать у @userinfobot   # строка — обязательная с описанием
  LOG_LEVEL:
    default: info
    description: Подробность логов — debug, info или warn
  SENTRY_DSN:
    required: false
    description: Отправка ошибок в Sentry; без неё ошибки только в логах
```

Пиши `env` словарём, а не списком имён: список `env: [A, B]` тоже работает, но без описаний человек не поймёт, что
вписать. Переменная с `default` и без явного `required` — необязательная.

## После файла

Скажи человеку, какие обязательные переменные заполнить, и как:

- **Хостинг airlok** — на странице приложения в дашборде или через MCP `set_env`, значения бери только у человека.
- **Свой сервер** — значения шифруются под ключ сервера, и airlok их не видит, поэтому **не передавай их через MCP**.
  Пусть человек заполнит их на странице приложения в дашборде или выполнит у себя в терминале
  `npx @airlok/cli env set ИМЯ --app <имя>` (CLI спросит значение, не показывая его) или
  `npx @airlok/cli env set --app <имя> --from .env` — взять значения из локального `.env`.

Никогда не выдумывай значения секретов, не подставляй заглушки и не коммить `.env` с настоящими значениями.
