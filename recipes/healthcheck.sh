#!/usr/bin/env bash
# Проверяет, что каждый сайт отвечает, и сообщает в Telegram, когда это меняется:
# «перестал отвечать» с причиной и «снова работает» с длительностью простоя.
# Запускается таймером da-healthcheck раз в минуту.
#
# Проверка идёт с самого сервера через nginx. Если сервер выключен целиком, сообщить
# он не сможет: для этого нужна внешняя проверка.

source "$(dirname "$0")/lib.sh"
set +e

STATE_DIR=/var/lib/deployassistant/health
FAILS_BEFORE_ALERT=2

diagnose() {
	local name=$1 dir=$APPS_ROOT/$1 reasons=() cid state
	cid=$(docker compose -f "$dir/compose.yml" --project-directory "$dir" ps -aq app </dev/null 2>/dev/null)
	if [[ -z $cid ]]; then
		reasons+=("контейнер приложения не найден")
	else
		state=$(docker inspect -f '{{.State.Status}} {{.State.OOMKilled}} {{.State.ExitCode}} {{.State.Restarting}}' "$cid" 2>/dev/null)
		read -r status oom code restarting <<<"$state"
		[[ $oom == true ]] && reasons+=("приложению не хватило памяти, система его остановила")
		[[ $restarting == true ]] && reasons+=("приложение падает и перезапускается по кругу")
		[[ $status != running && $oom != true ]] && reasons+=("контейнер приложения остановлен (${status}, код ${code})")
	fi
	systemctl is-active --quiet nginx || reasons+=("nginx не работает")
	local disk
	disk=$(df -P / | awk 'NR==2{gsub("%",""); print $5}')
	((disk >= 95)) && reasons+=("диск заполнен на ${disk}%")
	if ((${#reasons[@]} == 0)); then
		reasons+=("контейнер работает, но приложение отвечает ошибкой; смотрите логи: docker compose -f ${dir}/compose.yml logs app")
	fi
	printf '%s; ' "${reasons[@]}" | sed 's/; $//'
}

check_app() {
	local name=$1 conf domain code
	conf=/etc/nginx/sites-enabled/da-${name}.conf
	[[ -f $conf ]] || return
	domain=$(awk '$1=="server_name"{gsub(";", "", $2); print $2; exit}' "$conf")

	# Во время выкатки приложение перезапускается — это не простой.
	exec 7>"/run/da-deploy-${name}.lock"
	if ! flock -n 7; then
		exec 7>&-
		return
	fi
	exec 7>&-

	code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 \
		--resolve "${domain}:443:127.0.0.1" "https://${domain}/")

	local file=$STATE_DIR/$name status=up fails=0 since now first=no
	now=$(date +%s)
	if [[ -f $file ]]; then
		read -r status fails since <"$file"
	else
		first=yes
	fi
	since=${since:-$now}

	if [[ $code =~ ^[234] ]]; then
		if [[ $status == down ]]; then
			# Точность — интервал проверок (минута), поэтому округляем и пишем «около».
			local minutes=$(((now - since + 30) / 60))
			notify "✅ ${name}: сайт снова отвечает. Простой: около $((minutes > 0 ? minutes : 1)) мин. https://${domain}"
			report_health "$name" "$domain" up "$code" ""
			echo "${name}: снова отвечает"
		elif [[ $first == yes ]]; then
			# Первая проверка: в Telegram писать не о чем, а дашборду нужен начальный статус.
			report_health "$name" "$domain" up "$code" ""
		fi
		echo "up 0 ${now}" >"$file"
		return
	fi

	((fails == 0)) && since=$now
	fails=$((fails + 1))
	if [[ $status != down ]] && ((fails >= FAILS_BEFORE_ALERT)); then
		local reason
		reason=$(diagnose "$name")
		notify "🔴 ${name}: сайт не отвечает с $(date -d "@${since}" '+%H:%M %Z') (код ${code}). Причина: ${reason}. https://${domain}"
		report_health "$name" "$domain" down "$code" "$reason"
		echo "${name}: не отвечает (${code}): ${reason}"
		status=down
	fi
	echo "${status} ${fails} ${since}" >"$file"
}

install -d -m 755 "$STATE_DIR"
# Заодно досылаем в дашборд события, которые не ушли раньше: таймер и есть повтор.
flush_events
for dir in "$APPS_ROOT"/*/; do
	[[ -f $dir/compose.yml ]] && check_app "$(basename "$dir")"
done
