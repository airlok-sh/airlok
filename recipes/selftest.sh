#!/usr/bin/env bash
# Самопроверка песочницы приложения после деплоя: что обещано рецептом, то и проверяем.
#
#   selftest.sh /путь/к/проекту
#
# Все проверки идут на этом сервере и только к своему приложению.

source "$(dirname "$0")/lib.sh"
set +e

PASS=0
FAIL=0
PNG_1PX=iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==

# expect NAME REGEX ACTUAL
expect() {
	if [[ $3 =~ ^($2)$ ]]; then
		PASS=$((PASS + 1))
		printf '  %s✓%s %s\n' "$C_GRN" "$C_0" "$1"
	else
		FAIL=$((FAIL + 1))
		printf '  %s✗%s %s %s(ожидали %s, получили %s)%s\n' "$C_RED" "$C_0" "$1" "$C_DIM" "$2" "${3:-пусто}" "$C_0"
	fi
}

compose() { docker compose -f "$APP_DIR/compose.yml" --project-directory "$APP_DIR" "$@"; }

# Скрипт выполняется внутри контейнера приложения, от его пользователя.
inside_checks() {
	step "Изнутри контейнера приложения"
	local out
	# Проверки на sh: он есть в образе любого стека, а node или python — нет.
	# </dev/null: иначе exec съедает stdin вызывающего скрипта.
	# shellcheck disable=SC2016 # переменные раскрывает sh внутри контейнера
	out=$(compose exec -T app sh </dev/null -c '
t() {
	name=$1
	shift
	if res=$("$@" 2>&1); then
		echo "$name=$res"
		return
	fi
	case $res in
	*"Read-only file system"*) echo "$name=ERR:EROFS" ;;
	*"Operation not permitted"*) echo "$name=ERR:EPERM" ;;
	*"Permission denied"*) echo "$name=ERR:EACCES" ;;
	*) echo "$name=ERR:$res" ;;
	esac
}
script() { printf "#!/bin/sh\necho ran\n" >"$1" && chmod 755 "$1" && echo written; }
t write_code sh -c "echo x >/app/airlok-selftest && echo written"
t uid id -u
t docker_sock sh -c "test -e /var/run/docker.sock && echo true || echo false"
t host_apps sh -c "test -e /srv/apps && echo true || echo false"
t chown_root sh -c "echo x >/tmp/airlok-chown && chown 0:0 /tmp/airlok-chown && echo changed"
script /tmp/airlok-selftest.sh >/dev/null
t exec_tmp /tmp/airlok-selftest.sh
if [ -n "$UPLOAD_DIR" ]; then
	t write_uploads script "$UPLOAD_DIR/airlok-selftest.sh"
	t exec_uploads "$UPLOAD_DIR/airlok-selftest.sh"
	rm -f "$UPLOAD_DIR/airlok-selftest.sh"
fi
' 2>&1)
	val() { sed -n "s/^$1=//p" <<<"$out"; }

	expect "код приложения закрыт на запись" "ERR:EROFS" "$(val write_code)"
	expect "процесс работает не от root (uid ${APP_UID})" "$APP_UID" "$(val uid)"
	expect "нет доступа к Docker (docker.sock)" "false" "$(val docker_sock)"
	expect "не видны каталоги сервера и других приложений" "false" "$(val host_apps)"
	expect "нет прав менять владельца файлов" "ERR:EPERM" "$(val chown_root)"
	expect "из /tmp нельзя запустить программу" "ERR:EACCES" "$(val exec_tmp)"
	if ((UPLOADS_ENABLED)); then
		expect "в папку загрузок писать можно" "written" "$(val write_uploads)"
		expect "загруженный файл нельзя запустить как программу" "ERR:EACCES" "$(val exec_uploads)"
	fi
}

host_checks() {
	step "Сервер"
	local cid
	cid=$(compose ps -q app)
	expect "файловая система контейнера только на чтение" "true" "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$cid")"
	expect "у контейнера сняты все привилегии" "\[ALL\]" "$(docker inspect -f '{{.HostConfig.CapDrop}}' "$cid")"
	expect "no-new-privileges" ".*no-new-privileges.*" "$(docker inspect -f '{{.HostConfig.SecurityOpt}}' "$cid")"
	expect "контейнер не privileged" "false" "$(docker inspect -f '{{.HostConfig.Privileged}}' "$cid")"

	local writable
	# У симлинков права всегда 777, у них проверяем только владельца.
	writable=$(find "$(readlink -f "$APP_DIR/current")" \( ! -user root -o \( ! -type l \( -perm -o+w -o -perm -g+w \) \) \) -print -quit)
	expect "файлы кода принадлежат root и не доступны на запись" "" "$writable"

	local f perms=""
	for f in "$APP_DIR"/shared/*.env; do
		perms+="$(stat -c '%a' "$f") "
	done
	expect "файлы секретов с правами 600" "(600 )+" "$perms"

	if [[ $APP_KIND == web ]]; then
		local port_bind
		port_bind=$(ss -H -tln "( sport = :${APP_PORT} )" | awk '{print $4}' | sort -u | paste -sd' ' -)
		expect "порт приложения слушает только 127.0.0.1" "127\.0\.0\.1:${APP_PORT}" "$port_bind"
	else
		expect "у фонового приложения нет открытых портов" "\{\}|map\[\]" "$(docker inspect -f '{{json .HostConfig.PortBindings}}' "$cid")"
	fi
	if ((DB_ENABLED)); then
		expect "база не опубликована на сервере" "\{\}|map\[\]" "$(docker inspect -f '{{json .HostConfig.PortBindings}}' "$(compose ps -q db)")"
	fi
	if ((UPLOADS_ENABLED)); then
		expect "папка загрузок смонтирована с noexec" ".*noexec.*" "$(findmnt -no OPTIONS --target "$APP_DIR/shared/uploads")"
	fi
}

# get PATH [curl-аргументы] → код ответа; заголовки в $TMP/h
get() {
	local path=$1
	shift
	curl -sk -o "$TMP/body" -D "$TMP/h" -w '%{http_code}' --max-time 10 \
		--resolve "${APP_DOMAIN}:443:127.0.0.1" --resolve "${APP_DOMAIN}:80:127.0.0.1" "$@" "https://${APP_DOMAIN}${path}"
}
hdr() { grep -i "^$1:" "$TMP/h" | tail -1 | cut -d: -f2- | sed 's/^ *//' | tr -d '\r'; }

web_checks() {
	step "Сайт через nginx"
	expect "главная открывается" "200" "$(get /)"
	expect "nginx не сообщает версию" "nginx" "$(hdr server)"
	expect "заголовок X-Content-Type-Options" "nosniff" "$(hdr x-content-type-options)"
	expect "HTTP перенаправляет на HTTPS" "301" "$(curl -s -o /dev/null -w '%{http_code}' --resolve "${APP_DOMAIN}:80:127.0.0.1" "http://${APP_DOMAIN}/")"
	expect "скрытые файлы (/.env) не отдаются" "403|404" "$(get /.env)"
	expect "скрытые каталоги (/.git/config) не отдаются" "403|404" "$(get /.git/config)"

	((UPLOADS_ENABLED && UPLOADS_PUBLIC)) || return 0
	local dir=$APP_DIR/shared/uploads
	put() { install -o "$APP_USER" -g www-data -m 640 /dev/stdin "$dir/$1"; }
	printf '<html><script>alert(1)</script></html>' | put airlok-selftest.html
	printf '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>' | put airlok-selftest.svg
	base64 -d <<<"$PNG_1PX" | put airlok-selftest.png
	printf '%%PDF-1.4\n' | put airlok-selftest.pdf

	expect "листинг папки загрузок закрыт" "403|404" "$(get /uploads/)"
	expect "загруженный HTML не открывается как страница" "404" "$(get /uploads/airlok-selftest.html)"
	expect "загруженный SVG не отдаётся" "404" "$(get /uploads/airlok-selftest.svg)"
	if [[ " $UPLOAD_EXTS " == *" png "* ]]; then
		expect "картинка из загрузок отдаётся" "200" "$(get /uploads/airlok-selftest.png)"
		expect "картинка отдаётся с CSP sandbox" ".*sandbox.*" "$(hdr content-security-policy)"
		expect "картинка отдаётся с nosniff" "nosniff" "$(hdr x-content-type-options)"
	fi
	if [[ " $UPLOAD_EXTS " == *" pdf "* ]]; then
		expect "PDF из загрузок отдаётся" "200" "$(get /uploads/airlok-selftest.pdf)"
		expect "PDF только скачивается, а не открывается" "attachment" "$(hdr content-disposition)"
	fi
	rm -f "$dir"/airlok-selftest.*

	local big=$((UPLOAD_MAX_MB + 1))
	head -c $((big * 1024 * 1024)) /dev/zero >"$TMP/big"
	expect "файл больше ${UPLOAD_MAX_MB} МБ отклоняет nginx" "413" "$(get /api/upload -X POST --data-binary @"$TMP/big")"
}

main() {
	need_root
	local src=${1:-}
	[[ -d $src ]] || die "использование: $0 /путь/к/проекту"
	load_manifest "$src"
	[[ -n ${AIRLOK_APP_NAME:-} ]] && APP_NAME=$AIRLOK_APP_NAME
	[[ -n $APP_NAME ]] || APP_NAME=$(basename "$src" | tr '[:upper:]_. ' '[:lower:]---' | tr -cd 'a-z0-9-' | sed 's/^[^a-z]*//; s/-*$//' | cut -c1-31)
	APP_USER=app-$APP_NAME
	APP_DIR=$APPS_ROOT/$APP_NAME
	APP_UID=$(id -u "$APP_USER") || die "приложение ${APP_NAME} ещё не развёрнуто"
	[[ $APP_KIND == web ]] && APP_PORT=$(<"$APP_DIR/shared/port")
	[[ -n $APP_DOMAIN || $APP_KIND == worker ]] ||
		APP_DOMAIN=$(awk '$1=="server_name"{gsub(";", "", $2); print $2; exit}' "/etc/nginx/sites-enabled/airlok-${APP_NAME}.conf")
	TMP=$(mktemp -d)
	trap 'rm -rf "$TMP"' EXIT

	inside_checks
	host_checks
	[[ $APP_KIND == web ]] && web_checks

	printf '\n%sПроверок пройдено: %d, не пройдено: %d%s\n' "$C_B" "$PASS" "$FAIL" "$C_0"
	((FAIL == 0))
}

main "$@"
