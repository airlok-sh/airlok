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
ok() { printf '  %s✓%s %s\n' "$C_GRN" "$C_0" "$*"; }
note() { printf '  %s%s%s\n' "$C_DIM" "$*" "$C_0"; }
die() {
	DIE_MSG="$*"
	printf '%sОшибка:%s %s\n' "$C_RED" "$C_0" "$*" >&2
	exit 1
}

TELEGRAM_CONF=/etc/deployassistant/telegram.env
DASHBOARD_CONF=/etc/deployassistant/dashboard.env

# conf_value ФАЙЛ КЛЮЧ — значение из файла вида KEY=value. Файл читается, а не
# подключается через source: так его содержимое не может выполниться как код.
conf_value() { sed -n "s/^$2=//p" "$1" | tail -1; }

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

# dashboard_call МЕТОД ПУТЬ [JSON] — запрос к API агента в дашборде. Токен идёт
# через -K из stdin, а не аргументом, чтобы не светиться в ps. Печатает HTTP-код.
dashboard_call() {
	local method=$1 path=$2 body=${3:-} url token
	url=$(conf_value "$DASHBOARD_CONF" DA_API_URL)
	token=$(conf_value "$DASHBOARD_CONF" DA_AGENT_TOKEN)
	[[ -n $url && -n $token ]] || return 1
	local args=(-sS --max-time 10 -o /dev/null -w '%{http_code}' -X "$method" -K -)
	[[ -n $body ]] && args+=(-H 'Content-Type: application/json' --data-binary "$body")
	curl "${args[@]}" <<<"url = \"${url%/}${path}\"
header = \"Authorization: Bearer ${token}\"" 2>/dev/null
}

# События для дашборда сначала ложатся в очередь на диске, а уже потом отправляются:
# если дашборд недоступен, они дождутся связи. Имя файла начинается со времени в
# наносекундах, поэтому сортировка по имени — это порядок событий.
EVENTS_DIR=/var/lib/deployassistant/events
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

load_manifest() {
	local manifest=$1 vars
	[[ -f $manifest ]] || die "не найден манифест ${manifest}"
	vars=$(python3 "$RECIPES_DIR/manifest.py" "$manifest") || exit 1
	eval "$vars"
}
