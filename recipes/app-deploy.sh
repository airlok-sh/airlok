#!/usr/bin/env bash
# Песочница и деплой приложения по deployassistant.yml из корня проекта.
#
#   app-deploy.sh /путь/к/проекту
#
# Повторный запуск выкатывает новую версию. Если она не поднялась, остаётся предыдущая.

source "$(dirname "$0")/lib.sh"

BUILD_USER=da-build
NODE_IMAGE=node:22-bookworm-slim
PG_IMAGE=postgres:16
KEEP_RELEASES=3
IMAGE_EXTS="jpg jpeg png webp gif avif"

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
	local db_env=$APP_DIR/shared/db.env user_env=$APP_DIR/shared/user.env pw=""
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
		if ((DB_ENABLED)); then echo "DATABASE_URL=postgres://app:${pw}@db:5432/app"; fi
		if ((UPLOADS_ENABLED)); then
			echo "UPLOAD_DIR=/data/uploads"
			echo "UPLOAD_MAX_BYTES=$((UPLOAD_MAX_MB * 1024 * 1024))"
		fi
	} | write_if_changed "$APP_DIR/shared/app.env" 0600 || true
	[[ -f $user_env ]] || install -m 600 /dev/null "$user_env"
	ok "секреты в ${APP_DIR}/shared, права 600, в репозиторий не попадают"

	local name missing=()
	for name in $ENV_NAMES; do
		grep -q "^${name}=" "$user_env" || missing+=("$name")
	done
	if ((${#missing[@]})); then
		note "не заданы переменные: ${missing[*]} — впишите их в ${user_env} и запустите деплой снова"
	fi
}

prepare_release() {
	step "Релиз"
	local src=$1 id
	id=${DA_RELEASE_ID:-$(git -C "$src" rev-parse --short=12 HEAD 2>/dev/null || date +%Y%m%d-%H%M%S)}
	[[ $id =~ ^[A-Za-z0-9-]+$ ]] || die "некорректный id релиза: ${id}"
	RELEASE=$APP_DIR/releases/$id
	RELEASE_ID=$id
	# Релиз из GitHub однозначно задан коммитом, поэтому уже собранный можно взять
	# как есть: откат на прошлую версию занимает секунды, а не новую сборку.
	REUSED=0
	if [[ -n ${DA_RELEASE_ID:-} && -f ${RELEASE}.built && -d $RELEASE ]]; then
		REUSED=1
		ok "релиз ${id} уже собран, используем его"
		return
	fi
	rm -rf "$RELEASE" "${RELEASE}.built"
	install -d -m 755 "$RELEASE"
	rsync -a --exclude .git --exclude node_modules --exclude .next --exclude '.env*' "$src/" "$RELEASE/"
	chown -R "$BUILD_USER:$BUILD_USER" "$RELEASE"
	if [[ -n $(find "$src" -maxdepth 1 -name '.env*' ! -name '*.example') ]]; then
		note ".env из проекта не копируется: секреты задаются в ${APP_DIR}/shared/user.env"
	fi
	ok "релиз ${id}"
}

build() {
	((REUSED)) && return 0
	step "Сборка"
	local log=${RELEASE}.build.log
	# npm install выполняет скрипты чужих пакетов, поэтому у сборки нет ни секретов,
	# ни базы, ни прав: только папка релиза и доступ к реестру npm.
	if ! docker run --rm --name "da-build-${APP_NAME}" \
		--user "${BUILD_UID}:${BUILD_GID}" --cap-drop ALL --security-opt no-new-privileges \
		--memory 1g --pids-limit 512 \
		-e HOME=/tmp -e NEXT_TELEMETRY_DISABLED=1 -e npm_config_cache=/tmp/npm \
		-v "${RELEASE}:/app" -w /app "$NODE_IMAGE" \
		sh -c 'if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi && npm run build' \
		>"$log" 2>&1; then
		tail -30 "$log" >&2
		die "сборка не удалась, полный лог: ${log}"
	fi
	install -d "${RELEASE}/.next/cache"
	chown -R root:root "$RELEASE"
	# Права задаём явно, а не наследуем от исходников: временный каталог из mktemp
	# имеет 700, и приложение не смогло бы прочитать собственный код.
	chmod -R u+rwX,go+rX,go-w "$RELEASE"
	touch "${RELEASE}.built"
	ok "собрано в изолированном контейнере, код передан root и закрыт на запись"
}

write_compose() {
	{
		cat <<EOF
name: ${APP_NAME}
services:
  app:
    image: ${NODE_IMAGE}
    working_dir: /app
    command: ["node", "node_modules/next/dist/bin/next", "start", "-p", "3000", "-H", "0.0.0.0"]
    user: "${APP_UID}:${APP_GID}"
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    tmpfs:
      - /tmp:size=64m,noexec,nosuid,nodev
      - /app/.next/cache:size=128m,noexec,nosuid,nodev,uid=${APP_UID},gid=${APP_GID}
    env_file:
      - path: shared/app.env
      - path: shared/user.env
        required: false
    volumes:
      - ${RELEASE}:/app:ro
EOF
		if ((UPLOADS_ENABLED)); then echo "      - ${APP_DIR}/shared/uploads:/data/uploads"; fi
		cat <<EOF
    ports: ["127.0.0.1:${APP_PORT}:3000"]
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
	write_if_changed /usr/local/sbin/da-backup 0755 <<'EOF' || true
#!/usr/bin/env bash
# Дамп базы приложения: da-backup ИМЯ. Хранит 7 последних копий.
set -euo pipefail
dir=/srv/apps/$1
out=$dir/shared/backups/$(date +%Y%m%d-%H%M%S).sql.gz
umask 077
docker compose -f "$dir/compose.yml" --project-directory "$dir" exec -T db pg_dump -U app app </dev/null | gzip >"$out.tmp"
mv "$out.tmp" "$out"
ls -1t "$dir"/shared/backups/*.sql.gz | tail -n +8 | xargs -r rm -f
EOF
	local changed=no
	write_if_changed /etc/systemd/system/da-backup@.service 0644 <<'EOF' && changed=yes
[Unit]
Description=Бэкап базы %i
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/da-backup %i
EOF
	write_if_changed /etc/systemd/system/da-backup@.timer 0644 <<'EOF' && changed=yes
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
	systemctl enable --now "da-backup@${APP_NAME}.timer" >/dev/null 2>&1
	ok "ежедневный дамп в ${APP_DIR}/shared/backups, хранится 7 копий"
	note "копии лежат на этом же сервере: если пропадёт сервер, пропадут и они"
}

wait_healthy() {
	local i code
	for ((i = 0; i < 60; i++)); do
		code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${APP_PORT}/" || true)
		[[ $code =~ ^[23] ]] && return 0
		sleep 2
	done
	return 1
}

deploy() {
	step "Запуск"
	local prev="" log=$APP_DIR/shared/deploy.log
	[[ -L $APP_DIR/current ]] && prev=$(readlink -f "$APP_DIR/current")

	if ((DB_ENABLED)) && [[ -n $prev ]]; then
		/usr/local/sbin/da-backup "$APP_NAME" || die "бэкап базы перед выкаткой не удался, деплой остановлен"
		ok "бэкап базы перед выкаткой"
	fi

	write_compose
	if ((DB_ENABLED)); then
		compose up -d --wait db >"$log" 2>&1 || die "база не запустилась, лог: ${log}"
	fi
	# Пересоздаём всегда: папка релиза могла быть пересобрана под тем же коммитом.
	compose up -d --force-recreate --no-deps app >>"$log" 2>&1 || die "приложение не запустилось, лог: ${log}"

	if ! wait_healthy; then
		compose logs --tail 30 app >&2 || true
		if [[ -n $prev && $prev != "$RELEASE" ]]; then
			RELEASE=$prev
			write_compose
			compose up -d --force-recreate --no-deps app >>"$log" 2>&1 || true
			ROLLED_BACK=$(basename "$prev")
			die "новая версия не ответила за 2 минуты, вернули предыдущую (${ROLLED_BACK})"
		fi
		die "приложение не ответило за 2 минуты"
	fi
	ln -sfn "$RELEASE" "$APP_DIR/current"
	ok "приложение отвечает на 127.0.0.1:${APP_PORT}"

	local old
	for old in $(ls -1dt "$APP_DIR"/releases/*/ | tail -n +$((KEEP_RELEASES + 1))); do
		[[ ${old%/} == "$(readlink -f "$APP_DIR/current")" ]] && continue
		rm -rf "$old" "${old%/}.build.log" "${old%/}.built"
	done
}

nginx_site() {
	step "nginx и HTTPS"
	local ssl=/etc/ssl/deployassistant cert key
	cert=$ssl/${APP_NAME}.crt
	key=$ssl/${APP_NAME}.key
	if [[ ! -s $cert ]]; then
		install -d -m 700 "$ssl"
		openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=${APP_DOMAIN}" \
			-addext "subjectAltName=DNS:${APP_DOMAIN}" -keyout "$key" -out "$cert" 2>/dev/null
		chmod 600 "$key"
		note "сертификат самоподписанный; для настоящего домена: certbot --nginx -d ${APP_DOMAIN}"
	fi

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
            include snippets/da-security-headers.conf;
            add_header Content-Security-Policy \"default-src 'none'; sandbox\" always;
        }"
		fi
		if ((${#others[@]})); then
			uploads_block+="
        location ~* \\.($(IFS='|'; echo "${others[*]}"))\$ {
            include snippets/da-security-headers.conf;
            add_header Content-Security-Policy \"default-src 'none'; sandbox\" always;
            add_header Content-Disposition attachment always;
        }"
		fi
		uploads_block+="
        return 404;
    }"
	fi

	if write_if_changed "/etc/nginx/sites-available/da-${APP_NAME}.conf" 0644 <<EOF; then
server {
    listen 80;
    listen [::]:80;
    server_name ${APP_DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name ${APP_DOMAIN};

    ssl_certificate ${cert};
    ssl_certificate_key ${key};

    client_max_body_size ${UPLOAD_MAX_MB}m;
    include snippets/da-security-headers.conf;

    location ~ /\\.(?!well-known/) { deny all; }
    # Появляется после github-deploy.sh webhook; пока файла нет, include ничего не добавляет.
    include snippets/da-webhook*.conf;
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
		ln -sfn "/etc/nginx/sites-available/da-${APP_NAME}.conf" "/etc/nginx/sites-enabled/da-${APP_NAME}.conf"
		if ! nginx -t -q; then
			# Битый конфиг в sites-enabled сломал бы любой следующий reload nginx, в том числе чужих сайтов.
			rm -f "/etc/nginx/sites-enabled/da-${APP_NAME}.conf" "/etc/nginx/sites-available/da-${APP_NAME}.conf"
			die "nginx не принял конфиг сайта, конфиг убран, остальные сайты не затронуты"
		fi
		systemctl reload nginx
	fi
	ok "https://${APP_DOMAIN} → 127.0.0.1:${APP_PORT}, HTTP перенаправляется на HTTPS"
}

deploy_finished() {
	# RELEASE_ID, а не RELEASE: при откате RELEASE указывает уже на предыдущую версию.
	local code=$1 version=${RELEASE_ID:-} took=$((SECONDS - STARTED))
	local what="версия ${version}${DA_RELEASE_TITLE:+ «${DA_RELEASE_TITLE}»}" status
	if ((code == 0)); then
		status=success
		notify "✅ ${APP_NAME}: выкачена ${what} за ${took} с. https://${APP_DOMAIN}"
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
	# Выкатка, упавшая до создания релиза (например, уже идёт другая), id ещё не имеет.
	report_deploy "$APP_NAME" "$APP_DOMAIN" "${version:-unknown}" "${DA_RELEASE_TITLE:-}" "$status" "$took" "${DIE_MSG:-}"
}

main() {
	need_root
	local src=${1:-}
	[[ -d $src ]] || die "использование: $0 /путь/к/проекту"
	src=$(cd "$src" && pwd)
	load_manifest "$src/deployassistant.yml"
	APP_USER=app-$APP_NAME
	APP_DIR=$APPS_ROOT/$APP_NAME

	# Блокировка: две выкатки одного приложения не идут одновременно, а проверка
	# доступности не принимает перезапуск во время выкатки за простой.
	exec 8>"/run/da-deploy-${APP_NAME}.lock"
	flock -n 8 || die "выкатка ${APP_NAME} уже идёт"
	STARTED=$SECONDS
	RELEASE=""
	trap 'deploy_finished $?' EXIT

	ensure_users
	layout
	allocate_port
	secrets
	prepare_release "$src"
	build
	backup_setup
	deploy
	nginx_site
	printf '\n%sГотово:%s https://%s\n' "$C_B" "$C_0" "$APP_DOMAIN"
}

main "$@"
