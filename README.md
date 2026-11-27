<p align="center">
  <a href="https://www.airlok.ru">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset=".github/logo-white.svg">
      <img src=".github/logo.svg" alt="Airlok" height="56">
    </picture>
  </a>
</p>

<p align="center">
  Публикация сайтов, Telegram-ботов и бэкендов, собранных с ИИ, — на хостинге Airlok или на своём VPS.
</p>

<p align="center">
  <a href="https://www.npmjs.com/package/@airlok/cli"><img src="https://img.shields.io/npm/v/@airlok/cli?label=%40airlok%2Fcli" alt="npm"></a>
  <a href="https://app.airlok.ru/status"><img src="https://img.shields.io/badge/status-app.airlok.ru-2ea043" alt="Статус"></a>
  <a href="https://www.airlok.ru/docs/"><img src="https://img.shields.io/badge/docs-airlok.ru-0969da" alt="Документация"></a>
</p>

---

Airlok берёт проект из папки или GitHub, сам определяет стек, собирает и запускает его, выдаёт адрес с HTTPS и следит,
что сайт отвечает. База, загрузки файлов, задачи по расписанию и переменные — по галочкам в дашборде или в `airlok.yml`;
всё, что не отмечено, закрыто.

Два варианта, один и тот же движок:

- **Хостинг Airlok** — без своего сервера: `npx @airlok/cli deploy` из папки проекта или кнопка в дашборде.
- **Свой сервер** — любой чистый VPS на Ubuntu: Airlok ставит туда агента, Docker, nginx и файрвол одной командой, а
  публикует по push в GitHub или кнопкой. Всё, что запускается на сервере, лежит в этом репозитории.

Работает с Next.js, Node (Express, Nest, Fastify…), SvelteKit, Nuxt, Astro, Python (FastAPI, Flask, Django, Streamlit),
Go, статическими сайтами и фоновыми ботами. `airlok.yml` необязателен.

## Быстрый старт

```bash
npx @airlok/cli login     # вход через браузер
npx @airlok/cli deploy    # собрать и опубликовать текущую папку
```

Подключить свой сервер, не заходя на него:

```bash
npx @airlok/cli server connect root@IP-СЕРВЕРА --letsencrypt
```

Подключить Claude Code, Codex, Cursor или Antigravity — MCP-сервер и навыки Airlok, после чего агенту хватает «опубликуй
на Airlok» или «почему сайт отдаёт 500»:

```bash
npx @airlok/cli setup
```

Нужен Node.js 18.17+. Все команды и флаги — в [cli/README.md](cli/README.md).

## Что в репозитории

| Каталог | Что |
| --- | --- |
| [cli](cli) | `@airlok/cli`: публикация, проверка проекта, свой сервер, переменные, домены, устройства, подключение ИИ-агентов |
| [recipes](recipes) | агент и рецепты для своего сервера: настройка, песочница сборки, публикация, Let's Encrypt, бэкапы, cron, проверка доступности и метрики |
| [checkup](checkup) | проверка сервера только на чтение: SSH, открытые порты, файрвол, обновления, бэкапы, Docker, nginx |
| [schema](schema) | JSON-схема `airlok.yml` для редакторов и ИИ-агентов |
| [skills](skills) | навыки для ИИ-агентов: `airlok` и `airlok-yml`; отдельно — `npx skills add airlok-sh/airlok` |
| [examples](examples) | примеры проектов для хостинга и своего сервера |

Рецепты — обычные bash- и Python-скрипты: их можно прочитать целиком до запуска. Как устроен агент и что он делает на
сервере — [recipes/README.md](recipes/README.md).

## Безопасность

- Агент ставит только подписанные версии из `main`: по кнопке в дашборде или ночным автообновлением, которое берёт
  выпуск не моложе двух суток.
- Переменные приложения на своём сервере CLI шифрует на вашем компьютере под ключ агента; строгий режим сервера
  закрывает их и от самого Airlok.
- Нашли уязвимость — напишите в [поддержку](https://app.airlok.ru/support), а не в публичные issues.

## Ссылки

- Сайт и тарифы — [airlok.ru](https://www.airlok.ru)
- Документация — [airlok.ru/docs](https://www.airlok.ru/docs/)
- Дашборд — [app.airlok.ru](https://app.airlok.ru)
- Состояние сервиса — [app.airlok.ru/status](https://app.airlok.ru/status)
- Поддержка — [app.airlok.ru/support](https://app.airlok.ru/support), Telegram — [@airlok_support_bot](https://t.me/airlok_support_bot)

## Лицензия

[Apache-2.0](LICENSE).
