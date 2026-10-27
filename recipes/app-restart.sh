#!/usr/bin/env bash
# Перезапуск выкаченного приложения без сборки: та же версия, контейнер создаётся заново и
# перечитывает переменные окружения.
#
#   app-restart.sh ИМЯ
#
# Агент передаёт в AIRLOK_PANEL_ENV расшифрованные переменные из дашборда: ими заменяется
# shared/panel.env, как при выкатке. Переменные, встроенные в код при сборке, так не меняются.

source "$(dirname "$0")/lib.sh"

# crashing — контейнер приложения остановился, перезапускается или уже перезапускался.
crashing() {
	local running restarting restarts
	read -r running restarting restarts < <(docker inspect -f '{{.State.Running}} {{.State.Restarting}} {{.RestartCount}}' \
		"$(compose ps -aq app)" 2>/dev/null)
	[[ $running != true || $restarting == true || ${restarts:-0} -gt 0 ]]
}

compose() { docker compose -f "$APP_DIR/compose.yml" --project-directory "$APP_DIR" "$@" </dev/null; }

# wait_started ждёт ответа сайта до 2 минут, у фонового приложения — что контейнер не упал
# в первые секунды. Как после выкатки.
wait_started() {
	local port=$1 path=$2 i code
	if [[ -z $port ]]; then
		sleep 10
		! crashing
		return
	fi
	for ((i = 0; i < 60; i++)); do
		code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${port}${path:-/}" || true)
		health_ok "$code" "$path" && return 0
		((i >= 10 && i % 5 == 0)) && crashing && return 1
		sleep 2
	done
	return 1
}

main() {
	need_root
	local name=${1:-} port="" path release
	[[ $name =~ ^[a-z][a-z0-9-]{1,30}$ ]] || die "некорректное имя приложения: ${name}"
	APP_DIR=$APPS_ROOT/$name
	[[ -f $APP_DIR/compose.yml && -L $APP_DIR/current ]] || die "приложение ${name} на этом сервере ещё не выкатывали"

	# Та же блокировка, что у выкатки: перезапуск не пересечётся с ней, а проверка
	# доступности не примет его за простой.
	exec 8>"/run/airlok-deploy-${name}.lock"
	flock -w 60 8 || die "у ${name} идёт выкатка: перезапустите после неё"

	if [[ -n ${AIRLOK_PANEL_ENV:-} ]]; then
		write_if_changed "$APP_DIR/shared/panel.env" 0600 <"$AIRLOK_PANEL_ENV" || true
		ok "переменные из дашборда записаны"
	fi

	step "Перезапуск ${name}"
	compose up -d --force-recreate --no-deps app || die "контейнер приложения не создался"
	# Порт наружу публикуют только сайты: у фонового приложения его нет.
	grep -q '^    ports:' "$APP_DIR/compose.yml" && port=$(<"$APP_DIR/shared/port")
	release=$(readlink -f "$APP_DIR/current")
	path=$(release_health "$release")
	if ! wait_started "$port" "$path"; then
		compose logs --tail 30 app >&2 || true
		die "приложение не запустилось после перезапуска — причина в логе выше. Если ему не хватает переменных окружения, задайте их на странице приложения в дашборде airlok"
	fi
	printf '\n%sГотово:%s приложение %s перезапущено\n' "$C_B" "$C_0" "$name"
}

main "$@"
