# Общие функции рецептов. Подключается через source, сам не запускается.

set -euo pipefail
export LC_ALL=C.UTF-8 DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

RECIPES_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APPS_ROOT=/srv/apps

if [[ -t 1 ]]; then
	C_RED=$'\e[31m' C_GRN=$'\e[32m' C_DIM=$'\e[2m' C_B=$'\e[1m' C_0=$'\e[0m'
else
	C_RED="" C_GRN="" C_DIM="" C_B="" C_0=""
fi

step() { printf '%s→ %s%s\n' "$C_B" "$*" "$C_0"; }
# stage КЛЮЧ [ПОМЕТКА] и stage_skip КЛЮЧ [ПОМЕТКА] — отметки этапов выкатки для дашборда.
# Агент передаёт файл в AIRLOK_STAGE_FILE и по нему показывает ход выкатки; без агента
# отметки никуда не пишутся.
stage() { stage_mark running "$@"; }
stage_skip() { stage_mark skipped "$@"; }
stage_mark() {
	[[ -n ${AIRLOK_STAGE_FILE:-} ]] || return 0
	printf '%s %s %s %s\n' "$(date +%s)" "$2" "$1" "${*:3}" >>"$AIRLOK_STAGE_FILE" || true
}
ok() { printf '  %s✓%s %s\n' "$C_GRN" "$C_0" "$*"; }
note() { printf '  %s%s%s\n' "$C_DIM" "$*" "$C_0"; }
die() {
	DIE_MSG="$*"
	printf '%sОшибка:%s %s\n' "$C_RED" "$C_0" "$*" >&2
	exit 1
}

TELEGRAM_CONF=/etc/airlok/telegram.env
DASHBOARD_CONF=/etc/airlok/dashboard.env
# Флаг «служба агента изменилась»: при обновлении её перезапускает сам агент, уже
# отправив итог в дашборд.
AGENT_RESTART_FLAG=/run/airlok-agent/restart
# Настройки сервера, выбранные при установке: Let's Encrypt и т. п.
SERVER_CONF=/etc/airlok/server.env

# conf_value ФАЙЛ КЛЮЧ — значение из файла вида KEY=value. Файл читается, а не
# подключается через source: так его содержимое не может выполниться как код.
conf_value() { [[ -r $1 ]] || return 0; sed -n "s/^$2=//p" "$1" | tail -1; }

# conf_set ФАЙЛ КЛЮЧ ЗНАЧЕНИЕ — заменить или добавить KEY=value, остальное не трогать.
# Значение проверяет вызывающий: переводов строк и мусора в файле быть не должно.
conf_set() {
	local file=$1 key=$2 value=$3
	install -d -m 755 "$(dirname "$file")"
	{
		# grep без совпадений возвращает 1, а под set -e это оборвало бы запись.
		[[ -r $file ]] && { grep -v "^${key}=" "$file" || true; }
		printf '%s=%s\n' "$key" "$value"
	} | write_if_changed "$file" 0644 || true
}

# Версия рецептов для дашборда: дата и хеш коммита /opt/airlok, без git — dev.
agent_version() {
	git -C "$RECIPES_DIR" log -1 --format='%cd+%h' --date=format:'%Y.%m.%d' 2>/dev/null || echo dev
}

# notify ТЕКСТ — сообщение в Telegram, если бот подключён (notify.sh setup).
# Никогда не роняет вызывающий скрипт: уведомление вторично по отношению к деплою.
notify() {
	[[ -r $TELEGRAM_CONF ]] || return 0
	local token chat api
	token=$(conf_value "$TELEGRAM_CONF" TELEGRAM_BOT_TOKEN)
	chat=$(conf_value "$TELEGRAM_CONF" TELEGRAM_CHAT_ID)
	api=$(conf_value "$TELEGRAM_CONF" TELEGRAM_API)
	[[ -n $token && -n $chat ]] || return 0
	# URL с токеном передаём через -K из stdin, а не аргументом: аргументы видны в ps.
	curl -sS --max-time 10 -o /dev/null -K - \
		--data-urlencode "chat_id=${chat}" --data-urlencode "text=$1" \
		<<<"url = \"${api:-https://api.telegram.org}/bot${token}/sendMessage\"" >/dev/null 2>&1 || true
}

# dashboard_fetch МЕТОД ПУТЬ ФАЙЛ_ОТВЕТА [JSON|@ФАЙЛ] [ТАЙМАУТ] — запрос к API агента
# в дашборде, тело ответа пишется в файл. Токен идёт через -K из stdin, а не аргументом,
# чтобы не светиться в ps. Большое тело передаём как @файл: аргументы тоже видны в ps.
# Печатает HTTP-код.
dashboard_fetch() {
	local method=$1 path=$2 out=$3 body=${4:-} max_time=${5:-10} url token
	url=$(conf_value "$DASHBOARD_CONF" AIRLOK_API_URL)
	token=$(conf_value "$DASHBOARD_CONF" AIRLOK_AGENT_TOKEN)
	[[ -n $url && -n $token ]] || return 1
	local args=(-sS --max-time "$max_time" -o "$out" -w '%{http_code}' -X "$method" -K -)
	[[ -n $body ]] && args+=(-H 'Content-Type: application/json' --data-binary "$body")
	curl "${args[@]}" <<<"url = \"${url%/}${path}\"
header = \"Authorization: Bearer ${token}\"" 2>/dev/null
}

# dashboard_call МЕТОД ПУТЬ [JSON] — то же без тела ответа.
dashboard_call() { dashboard_fetch "$1" "$2" /dev/null "${3:-}"; }

# События для дашборда сначала ложатся в очередь на диске, а уже потом отправляются:
# если дашборд недоступен, они дождутся связи. Имя файла начинается со времени в
# наносекундах, поэтому сортировка по имени — это порядок событий.
EVENTS_DIR=/var/lib/airlok/events
MAX_QUEUED_EVENTS=1000
MAX_FAILED_EVENTS=100

# report_event JSON — поставить событие в очередь и попробовать отправить.
# Как и notify, никогда не роняет вызывающий скрипт.
report_event() {
	[[ -r $DASHBOARD_CONF ]] || return 0
	{
		install -d -m 700 "$EVENTS_DIR" "$EVENTS_DIR/failed"
		local name tmp
		name=$(date +%s%N)-$$-${RANDOM}.json
		tmp=$EVENTS_DIR/.${name}.tmp
		# Через временный файл: отправка не должна подхватить недописанное событие.
		printf '%s\n' "$1" >"$tmp" && mv "$tmp" "$EVENTS_DIR/$name"
		flush_events
	} || true
}

# flush_events — отправить очередь по порядку. Останавливается на первой временной
# ошибке, чтобы не перемешать события; следующую попытку делает healthcheck раз в минуту.
flush_events() {
	[[ -r $DASHBOARD_CONF && -d $EVENTS_DIR ]] || return 0
	(
		# Отправляет кто-то один: иначе два процесса пошлют одно и то же событие дважды.
		flock -n 9 || exit 0
		shopt -s nullglob
		local files=("$EVENTS_DIR"/*.json) f code
		if ((${#files[@]} > MAX_QUEUED_EVENTS)); then
			rm -f "${files[@]:0:${#files[@]}-MAX_QUEUED_EVENTS}"
			files=("${files[@]: -MAX_QUEUED_EVENTS}")
		fi
		for f in "${files[@]}"; do
			code=$(dashboard_call POST /agent/v1/events "$(<"$f")") || code=000
			case $code in
			2??) rm -f "$f" ;;
			# Дашборд отверг само событие: повтор не поможет, а очередь вставать не должна.
			400 | 413) mv -f "$f" "$EVENTS_DIR/failed/" ;;
			# Нет связи, ошибка дашборда, лимит или отозванный токен — ждём и пробуем позже.
			*) break ;;
			esac
		done
		local failed=("$EVENTS_DIR"/failed/*.json)
		if ((${#failed[@]} > MAX_FAILED_EVENTS)); then
			rm -f "${failed[@]:0:${#failed[@]}-MAX_FAILED_EVENTS}"
		fi
	) 9>"$EVENTS_DIR/.lock"
}

# JSON собираем в Python: заголовок коммита может содержать кавычки и переводы строк.
# id нужен дашборду, чтобы повтор из очереди не задвоил событие, occurred_at — чтобы
# в истории было время события, а не время его доставки.
EVENT_PY='
import datetime, json, sys, uuid
def event(kind, app, domain, payload):
    print(json.dumps({"id": str(uuid.uuid4()),
        "occurred_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "type": kind, "app": app, "domain": domain, kind: payload}))
'

# report_deploy ПРИЛОЖЕНИЕ ДОМЕН РЕЛИЗ ЗАГОЛОВОК СТАТУС СЕКУНДЫ СООБЩЕНИЕ
report_deploy() {
	[[ -r $DASHBOARD_CONF ]] || return 0
	report_event "$(python3 -c "$EVENT_PY"'
app, domain, release, title, status, took, message = sys.argv[1:]
event("deploy", app, domain, {"release": release[:64], "title": title[:500], "status": status,
    "duration_sec": int(took), "message": message[:2000]})
' "$@")"
}

# report_health ПРИЛОЖЕНИЕ ДОМЕН up|down HTTP-КОД ПРИЧИНА
report_health() {
	[[ -r $DASHBOARD_CONF ]] || return 0
	report_event "$(python3 -c "$EVENT_PY"'
app, domain, status, code, reason = sys.argv[1:]
event("health", app, domain, {"status": status, "http_code": int(code) if code.isdigit() else 0,
    "reason": reason[:1000]})
' "$@")"
}

need_root() { [[ $EUID -eq 0 ]] || die "запустите через sudo"; }

# write_if_changed PATH MODE — пишет stdin в файл, только если содержимое отличается.
# Возвращает 0, если файл изменён: по этому решаем, нужен ли reload сервиса.
write_if_changed() {
	local path=$1 mode=$2 tmp
	tmp=$(mktemp)
	cat >"$tmp"
	if [[ -f $path ]] && cmp -s "$tmp" "$path"; then
		rm -f "$tmp"
		return 1
	fi
	install -D -m "$mode" "$tmp" "$path"
	rm -f "$tmp"
	return 0
}

# Формат логов nginx для метрик: только время, код, длительность и размер ответа.
# Ни IP, ни адресов страниц — персональные данные посетителей серверу airlok не нужны.
# Нужен и server-base.sh, и app-deploy.sh: сайт с access_log … airlok без формата
# nginx не примет.
ensure_log_format() {
	write_if_changed /etc/nginx/conf.d/airlok-log.conf 0644 <<'EOF'
log_format airlok '$msec $status $request_time $body_bytes_sent';
EOF
}

# load_manifest КАТАЛОГ — настройки приложения из airlok.yml, а без него — по файлам проекта.
load_manifest() {
	local vars
	vars=$(python3 "$RECIPES_DIR/manifest.py" "$1") || exit 1
	eval "$vars"
}
