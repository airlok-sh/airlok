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
   - Postgres (pg, prisma, drizzle, psycopg, asyncpg, SQLAlchemy, Django, pgx, gorm, Laravel, Doctrine) →
     `capabilities.database.enabled: true`, код берёт адрес из `DATABASE_URL` (Laravel — из `DB_URL`, airlok задаёт
     обе). MySQL на хостинге нет: Laravel с MySQL переводи на `pgsql`. SQLite или файлы рядом с кодом не переживут перезапуск — предложи
     перейти на базу;
   - пользователи загружают файлы → `capabilities.uploads` с явным списком расширений, код пишет в `UPLOAD_DIR`;
   - задачи по расписанию (node-cron, APScheduler, celery beat, планировщик Laravel) → `capabilities.cron`: и на
     хостинге, и на своём сервере (у статического сайта на своём сервере задач нет). Для Laravel — задача `* * * * *` с `php artisan schedule:run`;
   - миграции (`prisma migrate deploy`, `alembic upgrade head`, `python manage.py migrate`, `goose up`,
     `php artisan migrate --force`) → `run.release`: выполняется один раз перед каждой новой версией.
   - Laravel: `APP_KEY` впиши с `secret: true` — в дашборде есть кнопка «Сгенерировать» с ключом нужного формата.
6. **Проверь запуск**: веб-приложение слушает `0.0.0.0` и порт из `PORT`. Захардкоженный порт или `localhost` —
   исправь в коде, это частая причина «сайт не отвечает».
7. **Адрес проверки `run.health`**: если в приложении есть `/health`, `/healthz`, `/api/health` или подобный —
   впиши его. Без него airlok проверяет главную и считает живым любой ответ, кроме 5xx; с ним — только 2xx или 3xx,
   и простой видно точнее. Не указывай адрес, которого нет, и адрес, закрытый паролем: выкатка вернёт прежнюю
   версию. Если главная медленная или зависит от внешних сервисов, а проверки нет — предложи добавить простой
   `GET /healthz`, отвечающий 200.
8. **Запиши файл** первой строкой со схемой (см. «Формат») и, если есть `.env.example`, дополни его теми же именами
   без значений — с комментарием-описанием.
9. **Проверь**: `npx @airlok/cli check --json`. Исправь всё из `errors` (выкатка с ними не пройдёт) и `warnings`
   (опечатки в полях — их сборка молча пропускает). Сверь `plan` (стек, команды запуска, `health`) и `env` с тем, что
   нашёл в коде: если стек или команда не те — допиши `app.stack`, `run.command` и т. п. Если приложение пойдёт на свой
   сервер, посмотри `server`: там то, чего на своём сервере нет. Без входа CLI проверить не сможет — тогда попроси
   человека выполнить `npx @airlok/cli login`.

## Формат

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/airlok-sh/airlok/main/schema/airlok.schema.json
version: 1
app:
  type: web                  # worker — бот или воркер без сайта
  stack: node                # static, nextjs, node, python, go (php и docker — только на хостинге) — если определилось не то
build:
  command: npm run build     # одна строка до 500 символов
  output: dist               # только для статики: каталог готового сайта
run:
  command: node dist/server.js
  release: npx prisma migrate deploy
  health: /healthz           # адрес проверки: отвечает 2xx, когда приложение готово
runtime:
  node: "22"                 # 20, 22, 24; python: 3.10–3.14; go: 1.25–1.27; php: 8.3–8.5
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

Только на хостинге: `app.stack: docker` и `build.dockerfile`. Свой сервер Dockerfile не читает и
собирает проект по его файлам — это не ошибка, не пройдёт только явный `app.stack: docker`. Только на своём сервере:
`app.domain` (домен сайта, например `shop.example.ru`) и `resources.memory_mb` (память контейнера, 128–8192 МБ, по
умолчанию 512 — подними для тяжёлых ботов и Puppeteer). Схема знает все поля и значения: если редактор подчёркивает
строку, поле написано неверно.

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
