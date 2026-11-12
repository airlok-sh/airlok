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
# Строгий режим (strict.py): файл меняет только человек на сервере, дашборду он недоступен.
POLICY_CONF=/etc/airlok/policy.env
# Закрытый ключ для переменных окружения из дашборда (envkey.py): сервер он не покидает.
ENV_KEY=/etc/airlok/env.key

# conf_value ФАЙЛ КЛЮЧ — значение из файла вида KEY=value. Файл читается, а не
# подключается через source: так его содержимое не может выполниться как код.
conf_value() { [[ -r $1 ]] || return 0; sed -n "s/^$2=//p" "$1" | tail -1; }

strict_mode() { [[ $(conf_value "$POLICY_CONF" AIRLOK_MODE) == strict ]]; }

# app_tail ЛОГ показывает последние строки упавшего приложения (compose задаёт сам рецепт) и печатает,
# где их искать. В строгом режиме они остаются только в файле на сервере: вывод рецепта уходит
# в дашборд, а в выводе приложений бывают адреса баз с паролями.
app_tail() {
	if strict_mode; then
		compose logs --tail 30 app >>"$1" 2>&1 || true
		note "строгий режим: последние строки приложения — в $1, на сервере" >&2
		echo "в $1 на сервере"
		return
	fi
	compose logs --tail 30 app >&2 || true
	echo "в логе выше"
}

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

# ensure_env_key — создать ключ для переменных из дашборда, если его ещё нет. Агент и
# healthcheck могут начать одновременно: ln не заменяет уже созданный файл.
ensure_env_key() {
	[[ -s $ENV_KEY ]] && return 0
	local tmp
	install -d -m 755 "$(dirname "$ENV_KEY")"
	tmp=$(mktemp "${ENV_KEY}.XXXXXX") || return 1
	if openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$tmp" 2>/dev/null; then
		chmod 600 "$tmp"
		ln "$tmp" "$ENV_KEY" 2>/dev/null || true
	fi
	rm -f "$tmp"
	[[ -s $ENV_KEY ]]
}

# env_key_public — открытый ключ (DER в base64) для дашборда; env_key_id — его отпечаток
# в том же виде, что показывает дашборд: по нему человек сверяет, что шифрует под этот сервер.
env_key_public() { openssl pkey -in "$ENV_KEY" -pubout -outform DER 2>/dev/null | base64 -w0; }
env_key_id() {
	local sum
	sum=$(openssl pkey -in "$ENV_KEY" -pubout -outform DER 2>/dev/null | openssl dgst -sha256 -binary | base64 -w0) || return 1
	echo "SHA256:${sum%%=*}"
}

# health_ok КОД ПУТЬ — прошла ли проверка доступности. Адрес из run.health приложение завело
# само, поэтому он должен отвечать 2xx или 3xx; главная страница — любым ответом, кроме 5xx:
# 401 у закрытого сайта или 404 у API без «/» значат, что приложение работает. Так же на хостинге.
health_ok() {
	if [[ -n $2 ]]; then
		[[ $1 =~ ^[23][0-9][0-9]$ ]]
	else
		[[ $1 =~ ^[234][0-9][0-9]$ ]]
	fi
}

# release_health РЕЛИЗ — адрес проверки из airlok.yml этого релиза; пусто — главная страница.
release_health() { cat "$1.health" 2>/dev/null || true; }

# Версия рецептов для дашборда: дата и хеш коммита /opt/airlok, без git — dev.
agent_version() {
	git -C "$RECIPES_DIR" log -1 --format='%cd+%h' --date=format:'%Y.%m.%d' 2>/dev/null || echo dev
}

# Открытые ключи, которыми подписаны версии Airlok. Файл лежит вне клона: новая версия из GitHub
# не назначает себе ключ сама, а сменить ключи может только версия, подписанная прежним.
SIGNERS_CONF=/etc/airlok/allowed_signers

has_signers() { [[ -r $1 ]] && grep -Eqv '^[[:space:]]*(#|$)' "$1"; }

# install_signers ФАЙЛ — ключи из ФАЙЛА на сервер. Файл без ключей не стирает те, что уже есть.
install_signers() {
	has_signers "$1" || return 0
	write_if_changed "$SIGNERS_CONF" 0644 <"$1" || true
}

# signed_commit КЛОН КОММИТ — коммит подписан SSH-ключом из SIGNERS_CONF. Подписи GPG и X.509 не
# принимаются: их проверка зависела бы от связки ключей root, которую Airlok не ведёт.
signed_commit() {
	git -C "$1" -c gpg.ssh.allowedSignersFile="$SIGNERS_CONF" -c gpg.program=false -c gpg.x509.program=false \
		verify-commit "$2" >/dev/null 2>&1
}

# latest_signed КЛОН — самый новый коммит main после стоящей версии, подписанный ключом из
# SIGNERS_CONF. Идёт по первым родителям: так это вершины main после слияний, а не коммиты веток.
latest_signed() {
	local rev
	while read -r rev; do
		signed_commit "$1" "$rev" && echo "$rev" && return 0
	done < <(git -C "$1" rev-list --first-parent --ancestry-path HEAD..FETCH_HEAD 2>/dev/null)
	return 1
}

# update_code КЛОН КОММИТ — перевести клон на коммит из только что полученной ветки main. Пока на
# сервере нет ключей (агент пришёл со старой версии, которая их не знала), подпись не проверяется.
update_code() {
	local dir=$1 rev short
	# Хеш разрешаем один раз: проверяется и ставится один и тот же объект, даже если короткий хеш
	# совпал с именем тега.
	rev=$(git -C "$dir" rev-parse -q --verify "${2}^{commit}") || die "коммита ${2} нет в ветке main"
	git -C "$dir" merge-base --is-ancestor "$rev" FETCH_HEAD 2>/dev/null || die "коммита ${2} нет в ветке main"
	# Версия уже стоит или старше стоящей: ставить нечего, и проверять подпись незачем.
	git -C "$dir" merge-base --is-ancestor "$rev" HEAD 2>/dev/null && return 0
	short=$(git -C "$dir" rev-parse --short "$rev")
	if has_signers "$SIGNERS_CONF" && ! signed_commit "$dir" "$rev"; then
		die "новая версия ${short} не подписана ключом Airlok, и сервер её не поставил: работает прежняя версия. Напишите в поддержку: https://app.airlok.ru/support"
	fi
	git -C "$dir" merge -q --ff-only "$rev" || die "не удалось перейти на версию ${short}: в ${dir} локальные правки или она не продолжает текущую. git -C ${dir} status"
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

# Формат логов nginx для метрик: время, код, длительность, размер ответа, метод, путь и
# время ответа приложения — в $request_time входят ещё приём тела запроса и отдача ответа
# медленному клиенту. Без IP и query-строки, а путь metrics.py отправляет в дашборд только
# шаблоном (/orders/:id) — персональные данные посетителей серверу airlok не нужны. В пути
# бывают пробелы, поэтому время приложения после него — в кавычках: кавычку в значениях
# nginx пишет как \x22. Нужен и server-base.sh, и app-deploy.sh: сайт с access_log … airlok
# без формата nginx не примет.
#
# Второй лог — посещения: только открытия страниц (GET с Accept: text/html или переход
# Next.js с заголовком RSC). В нём есть IP и браузер, но metrics.py раз в минуту превращает
# их в обезличенный хэш с солью суток и сразу стирает прочитанное: в дашборд уходят только
# хэши и счётчики.
#
# Здесь же $airlok_connection для WebSocket: map есть только на уровне http, а сайт без него
# nginx не примет.
ensure_log_format() {
	write_if_changed /etc/nginx/conf.d/airlok-log.conf 0644 <<'EOF'
map $http_upgrade $airlok_connection {
    default upgrade;
    '' close;
}
log_format airlok '$msec $status $request_time $body_bytes_sent $request_method $uri "$upstream_response_time"';
map "$request_method:$http_accept:$http_rsc" $airlok_visit {
    default 0;
    "~^GET:[^:]*text/html" 1;
    "~^GET:.*:1$" 1;
}
log_format airlok_visit escape=json '{"t":$msec,"s":$status,"ip":"$remote_addr","host":"$host","uri":"$request_uri",'
    '"ct":"$sent_http_content_type","dest":"$http_sec_fetch_dest","rsc":"$http_rsc","sp":"$http_sec_purpose",'
    '"pu":"$http_purpose","nrp":"$http_next_router_prefetch","ref":"$http_referer","ua":"$http_user_agent"}';
EOF
}

# load_manifest КАТАЛОГ — настройки приложения из airlok.yml, а без него — по файлам проекта.
load_manifest() {
	local vars
	vars=$(python3 "$RECIPES_DIR/manifest.py" "$1") || exit 1
	eval "$vars"
}

# start_cron_timers ИМЯ — снова запустить включённые задачи по расписанию приложения, которые
# остановил перенос данных. Без задач systemctl отвечает ошибкой — это не сбой.
start_cron_timers() {
	local timers
	timers=$(systemctl list-unit-files "airlok-cron@${1}_*.timer" --state=enabled --no-legend 2>/dev/null | awk '{print $1}') || true
	[[ -z $timers ]] || echo "$timers" | xargs systemctl start
}

# Контейнеры проекта Docker Compose ИМЯ: «id<TAB>имя<TAB>папка проекта». Airlok зовёт compose только
# с --project-directory /srv/apps/ИМЯ, а Compose без name: называет проект по папке, так что проект,
# поднятый руками из ~/ИМЯ, совпадает с приложением по имени. Свои контейнеры отличаем по папке.
project_containers() {
	docker ps -a --no-trunc --filter "label=com.docker.compose.project=$1" \
		--format $'{{.ID}}\t{{.Names}}\t{{.Label "com.docker.compose.project.working_dir"}}'
}

# foreign_containers ИМЯ КАТАЛОГ — чужие контейнеры проекта (те же три поля); без метки папки — чужой.
foreign_containers() {
	local out
	out=$(project_containers "$1") || return 1
	awk -F'\t' -v dir="$2" 'NF && $3 != dir' <<<"$out"
}

# own_containers ИМЯ КАТАЛОГ — id своих контейнеров проекта, в том числе разовых контейнеров задач.
own_containers() {
	local out
	out=$(project_containers "$1") || return 1
	awk -F'\t' -v dir="$2" '$3 == dir {print $1}' <<<"$out"
}

# Совет, если образы с Docker Hub не скачиваются. Зеркала Docker перечитывает по reload, без остановки
# контейнеров, а обновление агента дописывает в daemon.json только свои ключи.
HUB_MIRROR_ADVICE='Настройте зеркало: добавьте в /etc/docker/daemon.json "registry-mirrors": ["https://mirror.gcr.io"] (или зеркало вашего хостера), выполните sudo systemctl reload docker и проверьте, что зеркало видно в docker info (Registry Mirrors): если нет — в файле ошибка, и после перезагрузки сервера Docker не запустится. Обновления агента зеркало сохранят'

# hub_failed ЛОГ — в логе отказ Docker Hub: лимит запросов, запрет или нет связи. «Образ не найден» и
# «нет доступа» сюда не входят: это ошибка в имени или версии образа, зеркало её не исправит. Слои и
# конфиги образов Hub отдаёт с Cloudflare R2, и dockerd пишет об их сбое без имени хоста: «error
# pulling image configuration», «download failed after attempts», а у docker run — строкой «docker: …».
hub_failed() {
	local lines
	lines=$(grep -iE 'toomanyrequests|pull rate limit|Docker is a US company|(registry-1|auth|index)\.docker\.io|production\.cloudflare\.docker\.com|r2\.cloudflarestorage\.com|failed to resolve source metadata for docker\.io/|error pulling image configuration|download failed after attempts|failed to register layer|^docker: (read tcp|unexpected EOF|net/http|dial tcp)' "$1" 2>/dev/null |
		grep -viE 'pull access denied|manifest unknown|not found|repository does not exist|requested access to the resource is denied|\b401\b|unauthorized') || return 1
	grep -qiE 'toomanyrequests|pull rate limit|US company|\b(429|403|50[234])\b|forbidden|timeout|timed out|connection reset|connection refused|no such host|misbehaving|name resolution|deadline exceeded|network is unreachable|failed to do request|EOF' <<<"$lines"
}

# hub_hint ЛОГ — продолжение текста ошибки публикации, если виноват Docker Hub, иначе пусто.
hub_hint() {
	hub_failed "$1" || return 0
	printf '. Причина — Docker Hub: с этого сервера он недоступен или упёрся в лимит запросов, проект тут ни при чём. %s' "$HUB_MIRROR_ADVICE"
}
