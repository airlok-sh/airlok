#!/usr/bin/env bash
# Песочница и деплой приложения: по airlok.yml из корня проекта, а без него — по файлам
# проекта (Next.js, Node, Python, Go, статика), как на хостинге airlok.
#
#   app-deploy.sh /путь/к/проекту
#
# Повторный запуск выкатывает новую версию. Если она не поднялась, остаётся предыдущая.

source "$(dirname "$0")/lib.sh"

BUILD_USER=airlok-build
PG_IMAGE=postgres:16
# Статику отдаёт nginx без root внутри контейнера: тот же откат, проверки и изоляция,
# что у приложений.
STATIC_IMAGE=nginxinc/nginx-unprivileged:1.27-alpine
# Порт приложения внутри контейнера; снаружи он доступен только nginx сервера.
CONTAINER_PORT=8080
KEEP_RELEASES=3
IMAGE_EXTS="jpg jpeg png webp gif avif"
# Свой домен из дашборда: ASCII, кириллица уже в punycode (xn--p1ai для .рф).
DOMAIN_RE='^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63}|xn--[a-z0-9-]{1,59})$'

ensure_users() {
	step "Пользователи"
	local u
	for u in "$APP_USER" "$BUILD_USER"; do
		id "$u" >/dev/null 2>&1 ||
			useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$u"
	done
	APP_UID=$(id -u "$APP_USER")
	APP_GID=$(id -g "$APP_USER")
	BUILD_UID=$(id -u "$BUILD_USER")
	BUILD_GID=$(id -g "$BUILD_USER")
	ok "${APP_USER} (uid ${APP_UID}): без входа, без sudo"
	ok "сборка идёт от отдельного ${BUILD_USER}"
}

# Bind-маунт с noexec: что бы ни загрузили пользователи, запустить это как программу нельзя.
# Docker при монтировании в контейнер наследует флаги этого маунта.
mount_noexec() {
	local dir=$1 unit
	unit=$(systemd-escape --path --suffix=mount "$dir")
	if write_if_changed "/etc/systemd/system/${unit}" 0644 <<EOF; then
[Unit]
Description=noexec для загрузок ${APP_NAME}
Before=docker.service

[Mount]
What=${dir}
Where=${dir}
Type=none
Options=bind,noexec,nosuid,nodev

[Install]
WantedBy=multi-user.target
EOF
		systemctl daemon-reload
	fi
	systemctl enable --now "$unit" >/dev/null 2>&1
	[[ $(findmnt -no OPTIONS --target "$dir") == *noexec* ]] || die "не удалось смонтировать ${dir} с noexec"
}

layout() {
	step "Каталоги"
	install -d -m 755 -o root -g root "$APPS_ROOT" "$APP_DIR" "$APP_DIR/releases" "$APP_DIR/shared"
	install -d -m 700 -o root -g root "$APP_DIR/shared/backups"
	ok "код в ${APP_DIR}/releases: владелец root, приложению только чтение"
	if ((UPLOADS_ENABLED)); then
		# setgid на каталоге: новые файлы получают группу www-data, и nginx может их отдавать.
		install -d -m 2750 -o "$APP_USER" -g www-data "$APP_DIR/shared/uploads"
		mount_noexec "$APP_DIR/shared/uploads"
		ok "загрузки в ${APP_DIR}/shared/uploads: единственное место с записью, noexec"
	fi
}

allocate_port() {
	local f=$APP_DIR/shared/port p
	if [[ -s $f ]]; then
		APP_PORT=$(<"$f")
		return
	fi
	for ((p = 3100; p < 3300; p++)); do
		grep -qsx "$p" "$APPS_ROOT"/*/shared/port && continue
		[[ -n $(ss -H -tln "( sport = :${p} )") ]] && continue
		APP_PORT=$p
		echo "$p" >"$f"
		return
	done
	die "не нашли свободный порт в диапазоне 3100–3299"
}

secrets() {
	step "Секреты"
	local db_env=$APP_DIR/shared/db.env user_env=$APP_DIR/shared/user.env panel_env=$APP_DIR/shared/panel.env pw=""
	if ((DB_ENABLED)); then
		if [[ ! -s $db_env ]]; then
			printf 'POSTGRES_USER=app\nPOSTGRES_DB=app\nPOSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)" |
				write_if_changed "$db_env" 0600
			ok "сгенерирован пароль базы"
		fi
		pw=$(sed -n 's/^POSTGRES_PASSWORD=//p' "$db_env")
	fi

	{
		echo "NODE_ENV=production"
		echo "NEXT_TELEMETRY_DISABLED=1"
		echo "HOME=/tmp"
		echo "PORT=${CONTAINER_PORT}"
		echo "HOST=0.0.0.0"
		echo "HOSTNAME=0.0.0.0"
		if ((DB_ENABLED)); then echo "DATABASE_URL=postgres://app:${pw}@db:5432/app"; fi
		if ((UPLOADS_ENABLED)); then
			echo "UPLOAD_DIR=/data/uploads"
			echo "UPLOAD_MAX_BYTES=$((UPLOAD_MAX_MB * 1024 * 1024))"
		fi
	} | write_if_changed "$APP_DIR/shared/app.env" 0600 || true
	[[ -f $user_env ]] || install -m 600 /dev/null "$user_env"
	# Переменные из дашборда агент расшифровал перед выкаткой; файл заменяется целиком, так
	# что удалённая в дашборде переменная пропадёт. Выкатка без агента его не трогает.
	if [[ -n ${AIRLOK_PANEL_ENV:-} ]]; then
		write_if_changed "$panel_env" 0600 <"$AIRLOK_PANEL_ENV" || true
	fi
	[[ -f $panel_env ]] || install -m 600 /dev/null "$panel_env"
	ok "секреты в ${APP_DIR}/shared, права 600, в репозиторий не попадают"

	# Обязательные — из airlok.yml и .env.example проекта. Без них приложение упало бы при
	# запуске, а узнать об этом лучше до сборки, чем через несколько минут.
	local name missing=()
	for name in $ENV_NAMES; do
		grep -qE "^${name}=['\"]?[^'\"]" "$panel_env" "$user_env" || missing+=("$name")
	done
	if ((${#missing[@]})); then
		die "не заданы обязательные переменные: ${missing[*]}. Задайте их на странице приложения в дашборде airlok или впишите в ${user_env} в виде ИМЯ=значение и выкатите снова — что в них, описано в airlok.yml или .env.example проекта"
	fi

	# Переменные сборки: NEXT_PUBLIC_*, VITE_* и отмеченные build: true в airlok.yml — их
	# встраивают в код. Остальные значения сборка не получает: в ней работают скрипты чужих пакетов.
	mapfile -d '' -t BUILD_ENV < <(python3 "$RECIPES_DIR/envkey.py" build-env "${ENV_BUILD:-}" "${ENV_NO_BUILD:-}" "$user_env" "$panel_env")
	BUILD_ENV_SUM=""
	if ((${#BUILD_ENV[@]})); then
		BUILD_ENV_SUM=$(printf '%s\0' "${BUILD_ENV[@]}" | sha256sum | cut -d' ' -f1)
	fi
}

prepare_release() {
	step "Релиз"
	local src=$1 id
	id=${AIRLOK_RELEASE_ID:-$(git -C "$src" rev-parse --short=12 HEAD 2>/dev/null || date +%Y%m%d-%H%M%S)}
	[[ $id =~ ^[A-Za-z0-9-]+$ ]] || die "некорректный id релиза: ${id}"
	RELEASE=$APP_DIR/releases/$id
	RELEASE_ID=$id
	# Адрес проверки — у каждого релиза свой: при откате проверяется то, что было в его airlok.yml.
	printf '%s' "$HEALTH_PATH" >"${RELEASE}.health"
	# Релиз из GitHub однозначно задан коммитом и переменными сборки (их отпечаток лежит в
	# .built), поэтому уже собранный можно взять как есть: откат занимает секунды, а не новую сборку.
	REUSED=0
	if [[ -n ${AIRLOK_RELEASE_ID:-} && -f ${RELEASE}.built && -d $RELEASE ]]; then
		if [[ $(<"${RELEASE}.built") == "$BUILD_ENV_SUM" ]]; then
			REUSED=1
			ok "релиз ${id} уже собран, используем его"
			return
		fi
		note "переменные сборки изменились с прошлой сборки ${id} — собираем заново"
	fi
	rm -rf "$RELEASE" "${RELEASE}.built"
	install -d -m 755 "$RELEASE"
	rsync -a --exclude .git --exclude node_modules --exclude .next --exclude .venv --exclude __pycache__ \
		--exclude '.env*' "$src/" "$RELEASE/"
	chown -R "$BUILD_USER:$BUILD_USER" "$RELEASE"
	if [[ -n $(find "$src" -maxdepth 1 -name '.env*' ! -name '*.example') ]]; then
		note ".env из проекта не копируется: секреты задаются в ${APP_DIR}/shared/user.env"
	fi
	ok "релиз ${id}"
}

# Образы сборки и запуска: версии только из списка, который проверил detect.py.
images() {
	case $APP_STACK in
	python) BUILD_IMAGE=python:${PYTHON_VERSION}-slim-bookworm RUN_IMAGE=$BUILD_IMAGE ;;
	# Программа запускается в том же образе: он уже скачан для сборки, в нём корневые
	# сертификаты для HTTPS и та же glibc, с которой собиралась программа с cgo.
	go) BUILD_IMAGE=golang:${GO_VERSION}-bookworm RUN_IMAGE=$BUILD_IMAGE ;;
	static) BUILD_IMAGE=node:${NODE_VERSION}-bookworm-slim RUN_IMAGE=$STATIC_IMAGE ;;
	*) BUILD_IMAGE=node:${NODE_VERSION}-bookworm-slim RUN_IMAGE=$BUILD_IMAGE ;;
	esac
}

# build_container СКРИПТ ЛОГ — установка зависимостей и сборка в контейнере. Значения
# переменных сборки идут через окружение docker, а не аргументами: в ps их не видно. Агенту вывод
# нужен и в общем логе: дашборд показывает его по ходу выкатки. sh -x печатает каждую команду
# перед запуском: go build и go mod download молчат минутами, а так видно, какой шаг идёт.
build_container() {
	# Установка зависимостей выполняет скрипты чужих пакетов, поэтому у сборки нет ни
	# секретов, ни базы, ни прав: только папка релиза и доступ к реестрам пакетов.
	local pair passed=()
	for pair in "${BUILD_ENV[@]}"; do passed+=(-e "${pair%%=*}"); done
	local run=(env "${BUILD_ENV[@]}" docker run --rm --name "airlok-build-${APP_NAME}"
		--user "${BUILD_UID}:${BUILD_GID}" --cap-drop ALL --security-opt no-new-privileges
		--memory 1g --pids-limit 512
		-e HOME=/tmp -e NEXT_TELEMETRY_DISABLED=1 -e npm_config_cache=/tmp/npm -e PIP_CACHE_DIR=/tmp/pip
		-e PYTHONDONTWRITEBYTECODE=1 "${passed[@]}"
		-v "${RELEASE}:/app" -w /app "$BUILD_IMAGE" sh -xc "$1")
	if [[ ${AIRLOK_BUILD_OUTPUT:-} == 1 ]]; then
		"${run[@]}" 2>&1 | tee "$2"
	else
		"${run[@]}" >"$2" 2>&1
	fi
}

build() {
	if ((REUSED)); then
		stage_skip build "версия уже собрана"
		return 0
	fi
	step "Сборка: ${APP_FRAMEWORK}"
	stage build "$APP_FRAMEWORK"
	local log=${RELEASE}.build.log script pair names=()
	for pair in "${BUILD_ENV[@]}"; do names+=("${pair%%=*}"); done
	((${#names[@]})) && note "переменные сборки: ${names[*]}"
	script=$BUILD_INSTALL
	[[ -n $script && -n $BUILD_CMD ]] && script+=" && "
	script+=$BUILD_CMD
	if [[ -n $script ]] && ! build_container "$script" "$log"; then
		[[ ${AIRLOK_BUILD_OUTPUT:-} == 1 ]] || tail -30 "$log" >&2
		local outside
		outside=$(grep -oE "EACCES: permission denied, [a-z]+ '/[^']*'" "$log" | grep -vE "'/(app|tmp)(/|')" | head -1)
		if [[ -n $outside ]]; then
			die "сборка пишет за пределы папки проекта (${outside#*, }): так бывает в монорепозитории, где фронтенд собирается в каталог бэкенда. Выкатывайте каталог, в котором собирается всё приложение, или поправьте путь сборки. Полный лог: ${log}"
		fi
		die "сборка не удалась, полный лог: ${log}"
	fi
	[[ $APP_STACK == nextjs ]] && install -d "${RELEASE}/.next/cache"
	# Каталог сайта проверяем сразу: без index.html выкатывать нечего.
	[[ $APP_STACK == static ]] && { static_root >/dev/null || exit 1; }
	chown -R root:root "$RELEASE"
	# Права задаём явно, а не наследуем от исходников: временный каталог из mktemp
	# имеет 700, и приложение не смогло бы прочитать собственный код.
	chmod -R u+rwX,go+rX,go-w "$RELEASE"
	printf '%s' "$BUILD_ENV_SUM" >"${RELEASE}.built"
	ok "собрано в изолированном контейнере, код передан root и закрыт на запись"
}

static_root() {
	local built=()
	[[ -n $BUILD_CMD ]] && built=(built)
	python3 "$RECIPES_DIR/detect.py" static-output "$RELEASE" "$BUILD_OUTPUT" "${built[@]}"
}

# static_conf — сайт для nginx внутри контейнера. Есть 404.html — сайт многостраничный и
# свою страницу ошибки отдаёт сам; нет — это SPA, и любой путь ведёт на index.html.
static_conf() {
	local root fallback="/index.html"
	root=$(static_root) || exit 1
	[[ -f $RELEASE/$root/404.html ]] && fallback="=404"
	write_if_changed "$APP_DIR/shared/static.conf" 0644 <<EOF || true
server {
    listen ${CONTAINER_PORT};
    server_tokens off;
    root /app/${root};
    index index.html;
    error_page 404 /404.html;
    location / { try_files \$uri \$uri/ \$uri.html ${fallback}; }
}
EOF
}

# compose_command — команда запуска в compose.yml. JSON — это YAML, а $ удваиваем: иначе
# compose подставил бы вместо него переменные своего окружения.
compose_command() {
	case $APP_STACK in
	nextjs) echo '["node", "node_modules/next/dist/bin/next", "start", "-p", "'"$CONTAINER_PORT"'", "-H", "0.0.0.0"]' ;;
	static) echo '["nginx", "-g", "daemon off;"]' ;;
	*) python3 -c 'import json, sys; print(json.dumps(["sh", "-c", sys.argv[1]]).replace("$", "$$"))' "$RUN_CMD" ;;
	esac
}

write_compose() {
	{
		cat <<EOF
name: ${APP_NAME}
services:
  app:
    image: ${RUN_IMAGE}
    working_dir: /app
    command: $(compose_command)
    user: "${APP_UID}:${APP_GID}"
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    tmpfs:
      - /tmp:size=64m,noexec,nosuid,nodev
EOF
		if [[ $APP_STACK == nextjs ]]; then
			echo "      - /app/.next/cache:size=128m,noexec,nosuid,nodev,uid=${APP_UID},gid=${APP_GID}"
		fi
		if [[ $APP_STACK == python ]]; then
			cat <<EOF
    environment:
      PATH: /app/.venv/bin:/usr/local/bin:/usr/bin:/bin
      PYTHONUNBUFFERED: "1"
      PYTHONDONTWRITEBYTECODE: "1"
      GRADIO_SERVER_NAME: 0.0.0.0
      GRADIO_SERVER_PORT: "${CONTAINER_PORT}"
EOF
		fi
		cat <<EOF
    env_file:
      - path: shared/app.env
      - path: shared/user.env
        required: false
      - path: shared/panel.env
        required: false
    volumes:
      - ${RELEASE}:/app:ro
EOF
		if [[ $APP_STACK == static ]]; then echo "      - ${APP_DIR}/shared/static.conf:/etc/nginx/conf.d/default.conf:ro"; fi
		if ((UPLOADS_ENABLED)); then echo "      - ${APP_DIR}/shared/uploads:/data/uploads"; fi
		if [[ $APP_KIND == web ]]; then echo "    ports: [\"127.0.0.1:${APP_PORT}:${CONTAINER_PORT}\"]"; fi
		cat <<EOF
    mem_limit: ${MEMORY_MB}m
    pids_limit: 200
    restart: unless-stopped
EOF
		if ((DB_ENABLED)); then
			cat <<EOF
    depends_on:
      db:
        condition: service_healthy
  db:
    image: ${PG_IMAGE}
    env_file: [shared/db.env]
    volumes: ["pgdata:/var/lib/postgresql/data"]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 256m
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app -d app"]
      interval: 5s
      retries: 30
volumes:
  pgdata:
EOF
		fi
	} | write_if_changed "$APP_DIR/compose.yml" 0600 || true
}

# </dev/null: compose exec читает stdin и съел бы команды скрипта, который вызвал деплой.
compose() { docker compose -f "$APP_DIR/compose.yml" --project-directory "$APP_DIR" "$@" </dev/null; }

backup_setup() {
	((DB_ENABLED)) || return 0
	step "Бэкапы"
	write_if_changed /usr/local/sbin/airlok-backup 0755 <<'EOF' || true
#!/usr/bin/env bash
# Дамп базы приложения: airlok-backup ИМЯ. Хранит 7 последних копий.
set -euo pipefail
dir=/srv/apps/$1
out=$dir/shared/backups/$(date +%Y%m%d-%H%M%S).sql.gz
umask 077
docker compose -f "$dir/compose.yml" --project-directory "$dir" exec -T db pg_dump -U app app </dev/null | gzip >"$out.tmp"
mv "$out.tmp" "$out"
ls -1t "$dir"/shared/backups/*.sql.gz | tail -n +8 | xargs -r rm -f
EOF
	local changed=no
	write_if_changed /etc/systemd/system/airlok-backup@.service 0644 <<'EOF' && changed=yes
[Unit]
Description=Бэкап базы %i
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/airlok-backup %i
EOF
	write_if_changed /etc/systemd/system/airlok-backup@.timer 0644 <<'EOF' && changed=yes
[Unit]
Description=Ежедневный бэкап базы %i

[Timer]
OnCalendar=*-*-* 03:30
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
EOF
	[[ $changed == yes ]] && systemctl daemon-reload
	systemctl enable --now "airlok-backup@${APP_NAME}.timer" >/dev/null 2>&1
	ok "ежедневный дамп в ${APP_DIR}/shared/backups, хранится 7 копий"
	note "копии лежат на этом же сервере: если пропадёт сервер, пропадут и они"
}

# crashing — контейнер приложения упал: остановился, перезапускается или уже
# перезапускался. Так бывает, когда программа завершается при старте, например без
# нужных переменных окружения.
crashing() {
	local running restarting restarts
	read -r running restarting restarts < <(docker inspect -f '{{.State.Running}} {{.State.Restarting}} {{.RestartCount}}' \
		"$(compose ps -aq app)" 2>/dev/null)
	[[ $running != true || $restarting == true || ${restarts:-0} -gt 0 ]]
}

# wait_running — фоновое приложение запустилось и не упало в первые секунды: порта у
# него нет, и «работает» значит только, что контейнер жив.
wait_running() {
	sleep 10
	! crashing
}

# wait_healthy ждёт ответа приложения до 2 минут. Если контейнер падает раз за разом,
# ждать дальше незачем: ответа не будет.
wait_healthy() {
	[[ $APP_KIND == worker ]] && { wait_running; return; }
	local i code path
	path=$(release_health "$RELEASE")
	for ((i = 0; i < 60; i++)); do
		code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${APP_PORT}${path:-/}" || true)
		LAST_HEALTH_CODE=$code
		health_ok "$code" "$path" && return 0
		((i >= 10 && i % 5 == 0)) && crashing && return 1
		sleep 2
	done
	return 1
}

# release_command ПРЕДЫДУЩИЙ_РЕЛИЗ — run.release из airlok.yml (миграции базы): после бэкапа базы
# и до запуска новой версии, один раз на сборку, как на хостинге. Повторная выкатка того же
# собранного релиза её не повторяет. Если команда не прошла, прежняя версия продолжает работать.
release_command() {
	local prev=$1 done=$APP_DIR/shared/released
	[[ -n $RELEASE_CMD ]] || return 0
	if ((REUSED)) && [[ $(cat "$done" 2>/dev/null) == "$RELEASE_ID" ]]; then
		note "команда перед запуском для ${RELEASE_ID} уже выполнялась"
		return 0
	fi
	step "Перед запуском: ${RELEASE_CMD}"
	stage start "команда перед запуском"
	if ! python3 "$RECIPES_DIR/cron.py" release "$APP_NAME" "$RELEASE_CMD"; then
		# compose.yml уже указывает на новую версию: перезапуск из дашборда поднял бы именно её.
		if [[ -n $prev ]]; then
			RELEASE=$prev
			write_compose
		fi
		die "команда перед запуском (run.release) не прошла — новая версия не запущена${prev:+, работает прежняя}. Причина в выводе выше"
	fi
	printf '%s\n' "$RELEASE_ID" >"$done"
}

# cron_setup — таймеры задач по расписанию из airlok.yml выкаченной версии; убранные из него
# задачи выключаются. Версия уже работает, поэтому неудача здесь выкатку не роняет.
cron_setup() {
	local n
	printf '%s\n' "$CRON_JOBS" | write_if_changed "$APP_DIR/shared/cron.json" 0600 || true
	if ! n=$(python3 "$RECIPES_DIR/cron.py" sync "$APP_NAME"); then
		note "задачи по расписанию не настроены — причина выше, выкатите приложение снова"
		return 0
	fi
	((n)) || return 0
	step "Задачи по расписанию"
	ok "задач: ${n}, время московское, вывод — в логах приложения и в journalctl -u 'airlok-cron@${APP_NAME}_*'"
}

deploy() {
	step "Запуск"
	stage start
	local prev="" log=$APP_DIR/shared/deploy.log
	[[ -L $APP_DIR/current ]] && prev=$(readlink -f "$APP_DIR/current")

	if ((DB_ENABLED)) && [[ -n $prev ]]; then
		/usr/local/sbin/airlok-backup "$APP_NAME" || die "бэкап базы перед выкаткой не удался, деплой остановлен"
		ok "бэкап базы перед выкаткой"
	fi

	[[ $APP_STACK == static ]] && static_conf
	write_compose
	if ((DB_ENABLED)); then
		compose up -d --wait db >"$log" 2>&1 || die "база не запустилась, лог: ${log}"
	fi
	release_command "$prev"
	# Пересоздаём всегда: папка релиза могла быть пересобрана под тем же коммитом.
	compose up -d --force-recreate --no-deps app >>"$log" 2>&1 || die "приложение не запустилось, лог: ${log}"

	stage start "ждём, что приложение заработает"
	if ! wait_healthy; then
		compose logs --tail 30 app >&2 || true
		local crashed=0
		crashing && crashed=1
		if [[ -n $prev && $prev != "$RELEASE" ]]; then
			RELEASE=$prev
			[[ $APP_STACK == static ]] && static_conf
			write_compose
			compose up -d --force-recreate --no-deps app >>"$log" 2>&1 || true
			ROLLED_BACK=$(basename "$prev")
			((crashed)) && die "новая версия падает при запуске (причина в логе выше), вернули предыдущую (${ROLLED_BACK})"
			die "новая версия не запустилась, вернули предыдущую (${ROLLED_BACK})"
		fi
		if ((crashed)) || [[ $APP_KIND == worker ]]; then
			die "приложение падает при запуске — причина в логе выше. Если ему не хватает переменных окружения, задайте их на странице приложения в дашборде airlok или впишите в ${APP_DIR}/shared/user.env и выкатите снова"
		fi
		local path
		path=$(release_health "$RELEASE")
		if [[ -n $path && ${LAST_HEALTH_CODE:-000} != 000 ]]; then
			die "проверка ${path} из airlok.yml (run.health) ответила ${LAST_HEALTH_CODE}, а нужен 2xx или 3xx: поправьте этот адрес в приложении или в airlok.yml"
		fi
		die "приложение не ответило за 2 минуты: оно должно слушать порт из переменной PORT на 0.0.0.0"
	fi
	ln -sfn "$RELEASE" "$APP_DIR/current"
	if [[ $APP_KIND == worker ]]; then
		ok "фоновое приложение работает"
	else
		ok "приложение отвечает на 127.0.0.1:${APP_PORT}"
	fi

	local old
	for old in $(ls -1dt "$APP_DIR"/releases/*/ | tail -n +$((KEEP_RELEASES + 1))); do
		[[ ${old%/} == "$(readlink -f "$APP_DIR/current")" ]] && continue
		rm -rf "$old" "${old%/}.build.log" "${old%/}.built" "${old%/}.health"
	done
}

le_enabled() { [[ $(conf_value "$SERVER_CONF" LETSENCRYPT) == 1 ]]; }

nginx_site() {
	step "nginx и HTTPS"
	stage site "nginx и HTTPS"
	local ssl=/etc/ssl/airlok cert key le=/etc/letsencrypt/live/${APP_DOMAIN}
	cert=$ssl/${APP_NAME}.crt
	key=$ssl/${APP_NAME}.key
	if [[ ! -s $cert ]]; then
		install -d -m 700 "$ssl"
		openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=${APP_DOMAIN}" \
			-addext "subjectAltName=DNS:${APP_DOMAIN}" -keyout "$key" -out "$cert" 2>/dev/null
		chmod 600 "$key"
	fi
	# Самоподписанный остаётся запасным: без него nginx не поднял бы 443, пока
	# Let's Encrypt ещё не выдал настоящий.
	if [[ -s $le/fullchain.pem && -s $le/privkey.pem ]]; then
		cert=$le/fullchain.pem
		key=$le/privkey.pem
		TLS_KIND="Let's Encrypt"
	else
		TLS_KIND="самоподписанный"
		le_enabled || note "сертификат самоподписанный; выпустить настоящий: sudo ${RECIPES_DIR}/server-base.sh letsencrypt"
	fi
	ensure_log_format || true

	local uploads_block="" images=() others=() e
	if ((UPLOADS_ENABLED && UPLOADS_PUBLIC)); then
		for e in $UPLOAD_EXTS; do
			if [[ " $IMAGE_EXTS " == *" $e "* ]]; then images+=("$e"); else others+=("$e"); fi
		done
		uploads_block="
    # Отдаём только разрешённые расширения. CSP sandbox и nosniff не дают файлу
    # выполниться как страница сайта, остальное (не картинки) только скачивается.
    location /uploads/ {
        alias ${APP_DIR}/shared/uploads/;
        autoindex off;"
		if ((${#images[@]})); then
			uploads_block+="
        location ~* \\.($(IFS='|'; echo "${images[*]}"))\$ {
            include snippets/airlok-security-headers.conf;
            add_header Content-Security-Policy \"default-src 'none'; sandbox\" always;
        }"
		fi
		if ((${#others[@]})); then
			uploads_block+="
        location ~* \\.($(IFS='|'; echo "${others[*]}"))\$ {
            include snippets/airlok-security-headers.conf;
            add_header Content-Security-Policy \"default-src 'none'; sandbox\" always;
            add_header Content-Disposition attachment always;
        }"
		fi
		uploads_block+="
        return 404;
    }"
	fi

	# С nginx 1.25.1 (Ubuntu 26.04) HTTP/2 включается директивой http2, а параметр listen … http2
	# устарел; в 1.24 (Ubuntu 24.04) директивы ещё нет.
	local tls_listen="    listen 443 ssl http2;
    listen [::]:443 ssl http2;"
	if dpkg --compare-versions "$(nginx -v 2>&1 | sed -n 's|^nginx version: nginx/\([0-9.]*\).*|\1|p')" ge 1.25.1; then
		tls_listen="    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;"
	fi

	if write_if_changed "/etc/nginx/sites-available/airlok-${APP_NAME}.conf" 0644 <<EOF; then
server {
    listen 80;
    listen [::]:80;
    server_name ${APP_DOMAIN};
    include snippets/airlok-security-headers.conf;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}

server {
${tls_listen}
    server_name ${APP_DOMAIN};

    ssl_certificate ${cert};
    ssl_certificate_key ${key};

    client_max_body_size ${UPLOAD_MAX_MB}m;
    include snippets/airlok-security-headers.conf;
    access_log /var/log/nginx/airlok-${APP_NAME}.access.log airlok;

    location ~ /\\.(?!well-known/) { deny all; }
    # Появляется после github-deploy.sh webhook; пока файла нет, include ничего не добавляет.
    include snippets/airlok-webhook*.conf;
${uploads_block}

    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
		ln -sfn "/etc/nginx/sites-available/airlok-${APP_NAME}.conf" "/etc/nginx/sites-enabled/airlok-${APP_NAME}.conf"
		if ! nginx -t -q; then
			# Битый конфиг в sites-enabled сломал бы любой следующий reload nginx, в том числе чужих сайтов.
			rm -f "/etc/nginx/sites-enabled/airlok-${APP_NAME}.conf" "/etc/nginx/sites-available/airlok-${APP_NAME}.conf"
			die "nginx не принял конфиг сайта, конфиг убран, остальные сайты не затронуты"
		fi
		systemctl reload nginx
	fi
	ok "https://${APP_DOMAIN} → 127.0.0.1:${APP_PORT}, сертификат ${TLS_KIND}, HTTP перенаправляется на HTTPS"
}

# le_hint ЛОГ — что исправить, по выводу certbot. Чаще всего домен ещё не указывает на сервер.
le_hint() {
	local ip
	if grep -qi 'invalid email' "$1"; then
		echo "Let's Encrypt не принял email $(conf_value "$SERVER_CONF" LETSENCRYPT_EMAIL): укажите настоящий — sudo ${RECIPES_DIR}/server-base.sh letsencrypt ВАШ_EMAIL"
	elif grep -qiE 'too many|rate limit' "$1"; then
		echo "сработал лимит Let's Encrypt на выпуски для этого домена — попробуйте через час"
	else
		ip=$(hostname -I 2>/dev/null | awk '{print $1}')
		echo "Проверьте, что A-запись ${APP_DOMAIN} указывает на этот сервер${ip:+ (${ip})} и порт 80 открыт."
	fi
}

# Выпуск через webroot: nginx уже отдаёт /.well-known/acme-challenge/ на 80-м порту,
# поэтому certbot не трогает его конфиги. Неудача не роняет деплой: сайт работает
# с самоподписанным сертификатом, а следующая выкатка попробует снова.
issue_certificate() {
	le_enabled || return 0
	[[ -s /etc/letsencrypt/live/${APP_DOMAIN}/fullchain.pem ]] && return 0
	step "Let's Encrypt"
	stage site "сертификат Let's Encrypt"
	if ! command -v certbot >/dev/null; then
		note "certbot не установлен, сертификат остался самоподписанным: sudo ${RECIPES_DIR}/server-base.sh letsencrypt"
		return 0
	fi
	local email log=$APP_DIR/shared/letsencrypt.log contact=(--register-unsafely-without-email)
	email=$(conf_value "$SERVER_CONF" LETSENCRYPT_EMAIL)
	[[ -n $email ]] && contact=(-m "$email")
	if ! certbot certonly --webroot -w /var/www/html -d "$APP_DOMAIN" --non-interactive --agree-tos \
		"${contact[@]}" --deploy-hook "systemctl reload nginx" >"$log" 2>&1; then
		note "Let's Encrypt не выдал сертификат для ${APP_DOMAIN}, сайт работает с самоподписанным."
		note "$(le_hint "$log")"
		note "Следующая выкатка попробует снова. Подробности: ${log}"
		return 0
	fi
	ok "сертификат Let's Encrypt выпущен, продлевается сам (certbot.timer)"
	nginx_site
}

# Два сайта с одним server_name nginx принимает лишь с предупреждением и отдаёт домен первому
# по алфавиту: выкатка прошла бы успешно, а по адресу открывалось бы другое приложение.
ensure_domain_free() {
	local own=/etc/nginx/sites-enabled/airlok-${APP_NAME}.conf conf owner
	for conf in /etc/nginx/sites-enabled/*; do
		[[ -f $conf && $conf != "$own" ]] || continue
		awk -v d="$APP_DOMAIN" '$1 == "server_name" {
			for (i = 2; i <= NF; i++) { n = tolower($i); sub(/;$/, "", n); if (n == d) found = 1 }
		} END { exit !found }' "$conf" || continue
		owner=$(basename "$conf" .conf)
		if [[ $owner == airlok-* ]]; then
			die "домен ${APP_DOMAIN} уже занят приложением ${owner#airlok-} на этом сервере. Укажите другой домен (в дашборде или app.domain в airlok.yml) или отключите сайт старого приложения: rm /etc/nginx/sites-enabled/${owner}.conf && systemctl reload nginx"
		fi
		die "домен ${APP_DOMAIN} уже обслуживает сайт nginx ${conf}. Укажите другой домен (в дашборде или app.domain в airlok.yml) или отключите этот сайт"
	done
}

deploy_finished() {
	# RELEASE_ID, а не RELEASE: при откате RELEASE указывает уже на предыдущую версию.
	local code=$1 version=${RELEASE_ID:-} took=$((SECONDS - STARTED))
	local what="версия ${version}${AIRLOK_RELEASE_TITLE:+ «${AIRLOK_RELEASE_TITLE}»}" status domain=""
	if ((code == 0)); then
		status=success
		domain=$APP_DOMAIN
		notify "✅ ${APP_NAME}: выкачена ${what} за ${took} с.${APP_DOMAIN:+ https://${APP_DOMAIN}}"
	elif [[ -n ${ROLLED_BACK:-} ]]; then
		status=rolled_back
		notify "⚠️ ${APP_NAME}: ${what} не поднялась, вернули предыдущую (${ROLLED_BACK}). Сайт работает на ней. Логи: ${APP_DIR}/shared/deploy.log"
	elif [[ -L $APP_DIR/current ]]; then
		status=failed
		notify "❌ ${APP_NAME}: выкатка ${what} не удалась: ${DIE_MSG:-неизвестная ошибка}. Сайт остался на прежней версии."
	else
		status=failed
		notify "❌ ${APP_NAME}: первая выкатка не удалась: ${DIE_MSG:-неизвестная ошибка}."
	fi
	# Выкатка, упавшая до создания релиза (например, уже идёт другая), id ещё не имеет. Домен —
	# только у успешной: после неудачи nginx остался на прежнем адресе.
	report_deploy "$APP_NAME" "$domain" "${version:-unknown}" "${AIRLOK_RELEASE_TITLE:-}" "$status" "$took" "${DIE_MSG:-}"
}

# default_name — имя приложения по папке проекта: для выкатки руками без airlok.yml.
default_name() {
	local name
	name=$(basename "$1" | tr '[:upper:]_. ' '[:lower:]---' | tr -cd 'a-z0-9-' | sed 's/^[^a-z]*//; s/-*$//' | cut -c1-31)
	[[ $name =~ ^[a-z][a-z0-9-]{1,30}$ ]] || die "не получилось придумать имя по папке ${1}: укажите app.name в airlok.yml"
	echo "$name"
}

# default_domain — адрес, который работает без своего домена: ИМЯ.IP.sslip.io указывает на
# этот сервер, и на него можно выпустить сертификат Let's Encrypt.
default_domain() {
	local ip
	ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
	[[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "не нашли IP сервера: подключите свой домен в дашборде или укажите app.domain в airlok.yml"
	if [[ $ip =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.) ]]; then
		die "у сервера внутренний адрес ${ip}, снаружи по нему не открыть: подключите свой домен в дашборде или укажите app.domain в airlok.yml"
	fi
	echo "${APP_NAME}.${ip//./-}.sslip.io"
}

# panel_domain — свой домен из дашборда главнее app.domain из airlok.yml. Агент присылает его,
# когда A-запись уже указывает на сервер, а пустым — когда домена в дашборде нет. Выкатка без
# агента (руками или по webhook на сервере) берёт запомненный, иначе сайт ушёл бы с домена.
panel_domain() {
	if [[ -n ${AIRLOK_APP_DOMAIN+set} ]]; then
		PANEL_DOMAIN=$AIRLOK_APP_DOMAIN
	else
		PANEL_DOMAIN=$(cat "$APP_DIR/shared/panel.domain" 2>/dev/null || true)
	fi
	[[ -z $PANEL_DOMAIN || ($PANEL_DOMAIN =~ $DOMAIN_RE && ${#PANEL_DOMAIN} -le 253) ]] ||
		die "некорректный домен из дашборда: ${PANEL_DOMAIN}"
	[[ -n $PANEL_DOMAIN ]] || return 0
	if [[ $APP_KIND != web ]]; then
		note "у фонового приложения нет сайта: домен ${PANEL_DOMAIN} из дашборда не используется"
		return 0
	fi
	APP_DOMAIN=$PANEL_DOMAIN
}

# save_panel_domain — запомнить домен, который прислал агент; выкатка руками его не меняет.
save_panel_domain() {
	[[ -n ${AIRLOK_APP_DOMAIN+set} ]] || return 0
	if [[ -n $PANEL_DOMAIN ]]; then
		printf '%s\n' "$PANEL_DOMAIN" >"$APP_DIR/shared/panel.domain"
	else
		rm -f "$APP_DIR/shared/panel.domain"
	fi
}

main() {
	need_root
	local src=${1:-}
	[[ -d $src ]] || die "использование: $0 /путь/к/проекту"
	src=$(cd "$src" && pwd)
	load_manifest "$src"
	# Агент разворачивает под именем из дашборда: в монорепозитории у фронтенда и бэкенда
	# свои манифесты, но имена приложений на сервере не должны совпасть.
	if [[ -n ${AIRLOK_APP_NAME:-} ]]; then
		[[ $AIRLOK_APP_NAME =~ ^[a-z][a-z0-9-]{1,30}$ ]] || die "некорректное имя приложения: ${AIRLOK_APP_NAME}"
		APP_NAME=$AIRLOK_APP_NAME
	fi
	[[ -n $APP_NAME ]] || APP_NAME=$(default_name "$src")
	APP_DIR=$APPS_ROOT/$APP_NAME
	panel_domain
	if [[ $APP_KIND == web && -z $APP_DOMAIN ]]; then
		APP_DOMAIN=$(default_domain)
		[[ -f $src/airlok.yml ]] || note "airlok.yml нет: ${APP_FRAMEWORK}, адрес ${APP_DOMAIN}, без базы и загрузок"
	fi
	images
	APP_USER=app-$APP_NAME

	# Блокировка: две выкатки одного приложения не идут одновременно, а проверка
	# доступности не принимает перезапуск во время выкатки за простой.
	exec 8>"/run/airlok-deploy-${APP_NAME}.lock"
	flock -n 8 || die "выкатка ${APP_NAME} уже идёт"
	STARTED=$SECONDS
	RELEASE=""
	trap 'deploy_finished $?' EXIT

	# До сборки: занятый домен выяснится за секунду, а не после нескольких минут npm.
	[[ $APP_KIND == web ]] && ensure_domain_free
	ensure_users
	layout
	save_panel_domain
	[[ $APP_KIND == web ]] && allocate_port
	secrets
	prepare_release "$src"
	build
	backup_setup
	deploy
	cron_setup
	if [[ $APP_KIND == worker ]]; then
		printf '\n%sГотово:%s фоновое приложение %s работает, логи: docker compose -f %s/compose.yml logs app\n' "$C_B" "$C_0" "$APP_NAME" "$APP_DIR"
		return
	fi
	nginx_site
	issue_certificate
	printf '\n%sГотово:%s https://%s\n' "$C_B" "$C_0" "$APP_DOMAIN"
}

main "$@"
