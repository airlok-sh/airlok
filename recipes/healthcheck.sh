#!/usr/bin/env bash
# Проверяет, что каждый сайт отвечает, и сообщает в Telegram, когда это меняется:
# «перестал отвечать» с причиной и «снова работает» с длительностью простоя.
# Запускается таймером airlok-healthcheck раз в минуту.
#
# Проверка идёт с самого сервера через nginx. Если сервер выключен целиком, сообщить
# он не сможет: для этого нужна внешняя проверка.

source "$(dirname "$0")/lib.sh"
set +e

STATE_DIR=/var/lib/airlok/health
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

# worker_code — «ответ» фонового приложения: сайта у него нет, работает — значит, контейнер
# жив и не перезапускается по кругу.
worker_code() {
	local dir=$APPS_ROOT/$1 cid
	cid=$(docker compose -f "$dir/compose.yml" --project-directory "$dir" ps -aq app </dev/null 2>/dev/null)
	[[ -n $cid && $(docker inspect -f '{{.State.Running}} {{.State.Restarting}}' "$cid" 2>/dev/null) == "true false" ]] &&
		echo 200 || echo 000
}

check_app() {
	local name=$1 conf domain="" code what=сайт
	conf=/etc/nginx/sites-enabled/airlok-${name}.conf
	if [[ -f $conf ]]; then
		domain=$(awk '$1=="server_name"{gsub(";", "", $2); print $2; exit}' "$conf")
	elif grep -qs '^    ports:' "$APPS_ROOT/$name/compose.yml"; then
		# Сайт приложения отключён руками: проверять нечего.
		return
	else
		what="фоновое приложение"
	fi

	# Во время выкатки приложение перезапускается — это не простой.
	exec 7>"/run/airlok-deploy-${name}.lock"
	if ! flock -n 7; then
		exec 7>&-
		return
	fi
	exec 7>&-

	local path=""
	if [[ -n $domain ]]; then
		path=$(release_health "$(readlink -f "$APPS_ROOT/$name/current")")
		code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 \
			--resolve "${domain}:443:127.0.0.1" "https://${domain}${path:-/}")
	else
		code=$(worker_code "$name")
	fi

	local file=$STATE_DIR/$name status=up fails=0 since now first=no
	now=$(date +%s)
	if [[ -f $file ]]; then
		read -r status fails since <"$file"
	else
		first=yes
	fi
	since=${since:-$now}

	if health_ok "$code" "$path"; then
		if [[ $status == down ]]; then
			# Точность — интервал проверок (минута), поэтому округляем и пишем «около».
			local minutes=$(((now - since + 30) / 60))
			notify "✅ ${name}: ${what} снова работает. Простой: около $((minutes > 0 ? minutes : 1)) мин.${domain:+ https://${domain}}"
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
		notify "🔴 ${name}: ${what} не работает с $(date -d "@${since}" '+%H:%M %Z') (код ${code}). Причина: ${reason}.${domain:+ https://${domain}}"
		report_health "$name" "$domain" down "$code" "$reason"
		echo "${name}: не отвечает (${code}): ${reason}"
		status=down
	fi
	echo "${status} ${fails} ${since}" >"$file"
}

HEARTBEAT_PY='
import json, os, sys

version, password, letsencrypt, env_key, ssh_report = sys.argv[1:6]
mem = {}
with open("/proc/meminfo") as f:
    for line in f:
        key, value = line.split(":", 1)
        mem[key] = int(value.split()[0])
os_name = ""
try:
    with open("/etc/os-release") as f:
        for line in f:
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip().strip("\"")
except OSError:
    pass
with open("/proc/uptime") as f:
    uptime = int(float(f.read().split()[0]))
disk = os.statvfs("/")
try:
    with open(ssh_report, encoding="utf-8") as f:
        ssh = json.load(f)
except (OSError, ValueError):
    ssh = {}
print(json.dumps({
    **{k: v for k, v in ssh.items() if k in ("ssh_keys", "ssh_users", "ssh_logins")},
    "agent_version": version,
    "os": os_name,
    "uptime_sec": uptime,
    "load1": round(os.getloadavg()[0], 2),
    "cpus": os.cpu_count() or 1,
    "mem_total_mb": mem["MemTotal"] // 1024,
    "mem_used_mb": (mem["MemTotal"] - mem.get("MemAvailable", mem["MemFree"])) // 1024,
    "disk_total_mb": disk.f_blocks * disk.f_frsize >> 20,
    "disk_used_mb": (disk.f_blocks - disk.f_bfree) * disk.f_frsize >> 20,
    "ssh_password_login": {"true": True, "false": False}.get(password),
    "letsencrypt": letsencrypt == "true",
    "env_key": env_key,
}))
'

# Отметка «сервер на связи» с его состоянием: о сайтах healthcheck сообщает только при
# изменениях, и без неё молчащий исправный сервер выглядел бы в дашборде пропавшим. В ней же —
# опись SSH-ключей и новые входы: тело бывает в сотни килобайт, поэтому идёт из файла.
heartbeat() {
	local conf password=null le=false key="" body=$STATE_DIR/heartbeat.json ssh=$STATE_DIR/ssh-report.json code
	if conf=$(sshd -T 2>/dev/null); then
		password=false
		grep -qxE '(passwordauthentication|kbdinteractiveauthentication) yes' <<<"$conf" && password=true
	fi
	[[ $(conf_value "$SERVER_CONF" LETSENCRYPT) == 1 ]] && le=true
	ensure_env_key && key=$(env_key_public)
	# В отчёте IP тех, кто входил на сервер, — файлы только для root.
	install -m 600 /dev/null "$ssh" && install -m 600 /dev/null "$body" || return
	timeout 60 python3 "$RECIPES_DIR/sshkeys.py" report "$STATE_DIR" >"$ssh" 2>/dev/null || echo '{}' >"$ssh"
	python3 -c "$HEARTBEAT_PY" "$(agent_version)" "$password" "$le" "$key" "$ssh" >"$body" || return
	code=$(dashboard_call POST /agent/v1/heartbeat "@${body}") || code=000
	# Дашборд старше описи ключей не примет большое тело: отмечаемся без неё, опись уйдёт позже.
	if [[ $code == 413 ]]; then
		python3 -c "$HEARTBEAT_PY" "$(agent_version)" "$password" "$le" "$key" /dev/null >"$body" || return
		code=$(dashboard_call POST /agent/v1/heartbeat "@${body}") || code=000
	elif [[ $code == 2?? ]]; then
		python3 "$RECIPES_DIR/sshkeys.py" report-sent "$STATE_DIR"
	fi
	# Дашборд старой версии heartbeat не знает — отмечаемся, как раньше.
	[[ $code == 404 ]] && dashboard_call GET /agent/v1/ping >/dev/null
}

install -d -m 755 "$STATE_DIR"
[[ -r $DASHBOARD_CONF ]] && heartbeat
# Служба логов появилась позже агента. update.sh старой версии дорабатывает из памяти и о ней
# не знает, поэтому после такого обновления её ставит проверка: она уже из новых рецептов.
if [[ -r $DASHBOARD_CONF && ! -e /etc/systemd/system/airlok-logs.service ]]; then
	"$RECIPES_DIR/notify.sh" install-logs >/dev/null
fi
# Заодно досылаем в дашборд события, которые не ушли раньше: таймер и есть повтор.
flush_events
for dir in "$APPS_ROOT"/*/; do
	[[ -f $dir/compose.yml ]] && check_app "$(basename "$dir")"
done
# Метрики вторичны: зависший запрос к дашборду не должен задерживать следующую проверку.
timeout 40 python3 "$RECIPES_DIR/metrics.py" || true
