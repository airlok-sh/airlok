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
	# </dev/null: иначе exec съедает stdin вызывающего скрипта.
	out=$(compose exec -T app node </dev/null -e '
const fs = require("fs"), cp = require("child_process");
const t = (name, fn) => { let v; try { v = fn(); } catch (e) { v = "ERR:" + (e.code || e.message); } console.log(name + "=" + v); };
const script = "#!/bin/sh\necho ran\n";
t("write_code", () => { fs.writeFileSync("/app/airlok-selftest", "x"); return "written"; });
t("uid", () => process.getuid());
t("docker_sock", () => fs.existsSync("/var/run/docker.sock"));
t("host_apps", () => fs.existsSync("/srv/apps"));
t("chown_root", () => { fs.writeFileSync("/tmp/airlok-chown", "x"); fs.chownSync("/tmp/airlok-chown", 0, 0); return "changed"; });
t("exec_tmp", () => { fs.writeFileSync("/tmp/airlok-selftest.sh", script, { mode: 0o755 }); return cp.execFileSync("/tmp/airlok-selftest.sh").toString().trim(); });
if (process.env.UPLOAD_DIR) {
  const f = process.env.UPLOAD_DIR + "/airlok-selftest.sh";
  t("write_uploads", () => { fs.writeFileSync(f, script, { mode: 0o755 }); return "written"; });
  t("exec_uploads", () => cp.execFileSync(f).toString().trim());
  try { fs.unlinkSync(f); } catch {}
}
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

	local port_bind
	port_bind=$(ss -H -tln "( sport = :${APP_PORT} )" | awk '{print $4}' | sort -u | paste -sd' ' -)
	expect "порт приложения слушает только 127.0.0.1" "127\.0\.0\.1:${APP_PORT}" "$port_bind"
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
	load_manifest "$src/airlok.yml"
	APP_USER=app-$APP_NAME
	APP_DIR=$APPS_ROOT/$APP_NAME
	APP_UID=$(id -u "$APP_USER") || die "приложение ${APP_NAME} ещё не развёрнуто"
	APP_PORT=$(<"$APP_DIR/shared/port")
	TMP=$(mktemp -d)
	trap 'rm -rf "$TMP"' EXIT

	inside_checks
	host_checks
	web_checks

	printf '\n%sПроверок пройдено: %d, не пройдено: %d%s\n' "$C_B" "$PASS" "$FAIL" "$C_0"
	((FAIL == 0))
}

main "$@"
