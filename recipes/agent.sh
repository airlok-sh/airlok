#!/usr/bin/env bash
# Агент: забирает команды из дашборда долгим опросом и выполняет их по одной.
#
# Работает службой airlok-agent от root, потому что деплою нужны его права. Умеет только
# выкатить коммит из GitHub и обновиться сам, когда это подтвердил человек в дашборде:
# произвольную команду дашборд прислать не может, а каждое поле команды проверяется здесь
# заново — значения доходят до команд от root.
# Сервер сам ходит в дашборд, входящих соединений агент не принимает.

source "$(dirname "$0")/lib.sh"
set +e

WAIT=25
# tmpfs: в ответе дашборда токен GitHub, на диск он попадать не должен.
RUN_DIR=/run/airlok-agent
STATE_DIR=/var/lib/airlok/agent
DEPLOY_TIMEOUT=3600
UPDATE_TIMEOUT=1800
PROGRESS_EVERY=3
# Шифротексты из команды и расшифрованные значения — только на tmpfs и только на время выкатки.
ENV_JSON=$RUN_DIR/env.json
PANEL_ENV=$RUN_DIR/panel.env

SELF=$(readlink -f "$0")
SELF_INODE=$(stat -c %i "$SELF" 2>/dev/null)

# Разбор команды. Поля выводятся через NUL в фиксированном порядке и в shell не
# исполняются; первое поле — ok или bad, второе — id команды (если его удалось понять),
# третье у ok — вид команды. Зашифрованные переменные из дашборда пишутся в файл из
# аргумента, последнее поле выкатки — 1, если они пришли.
PARSE_PY='
import json, re, sys

UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
REF = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]*$")
# Сегмент каталога не начинается с точки или дефиса: так отсекаются «..», скрытые
# каталоги и значения, похожие на опции.
SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
# Токены установки GitHub App бывают и короткими ghs_…, и длинными, с точками (около 400 символов).
TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,2048}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
SEALED = re.compile(r"^v1:[A-Za-z0-9+/=]{1,65536}$")


def emit(*fields):
    sys.stdout.write("".join(f.replace("\0", "") + "\0" for f in fields))
    sys.exit(0)


try:
    cmd = json.load(sys.stdin)
except ValueError:
    emit("bad", "", "ответ дашборда не разобрать")
if not isinstance(cmd, dict):
    emit("bad", "", "ответ дашборда не разобрать")
cid = cmd.get("id")
if not isinstance(cid, str) or not UUID.match(cid):
    emit("bad", "", "у команды нет корректного id")
kind = cmd.get("kind")
if kind == "update":
    emit("ok", cid, "update")
if kind != "deploy":
    emit("bad", cid, "агент не умеет выполнять команду %r: обновите рецепты на сервере" % str(kind)[:40])
d = cmd.get("deploy")
if not isinstance(d, dict):
    emit("bad", cid, "в команде нет параметров выкатки")


def text(key):
    v = d.get(key)
    if v is None:
        return ""
    if not isinstance(v, str):
        emit("bad", cid, "поле %s должно быть строкой" % key)
    return v


app, repo, sha, ref, root, title, token = (
    text(k) for k in ("app", "repo", "sha", "ref", "root_dir", "title", "github_token"))
if not NAME.match(app):
    emit("bad", cid, "некорректное имя приложения")
parts = repo.split("/")
if len(parts) != 2 or not all(REPO_PART.match(p) and p not in (".", "..") for p in parts):
    emit("bad", cid, "некорректный репозиторий")
if not SHA.match(sha):
    emit("bad", cid, "некорректный коммит")
if ref and (not REF.match(ref) or ".." in ref):
    emit("bad", cid, "некорректная ветка")
if root and not all(SEGMENT.match(s) for s in root.split("/")):
    emit("bad", cid, "некорректный каталог в репозитории")
if token and not TOKEN.match(token):
    emit("bad", cid, "некорректный токен GitHub")
# Нет поля — у дашборда нет ключа сервера или он старый: файл переменных не трогаем.
env = d.get("env")
if env is not None:
    if not isinstance(env, list) or len(env) > 100 or not all(
            isinstance(v, dict) and isinstance(v.get("name"), str) and ENV_NAME.match(v["name"])
            and isinstance(v.get("value"), str) and SEALED.match(v["value"]) for v in env):
        emit("bad", cid, "некорректные переменные окружения в команде")
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        json.dump([{"name": v["name"], "value": v["value"]} for v in env], f)
emit("ok", cid, "deploy", app, repo, sha, ref, root, " ".join(title.split())[:200], token, "0" if env is None else "1")
'

# Тело запроса к дашборду в JSON: режим progress — хвост лога и этапы идущей выкатки,
# result — итог. Сообщение итога — строка «Ошибка: …» или «Готово: …» из вывода, лог —
# хвост вывода, потому что причина неудачи обычно в последних строках. Этапы собираются из
# отметок рецептов (stage в lib.sh): без отметок их нет, и дашборд оставляет прежние.
PAYLOAD_PY='
import datetime, json, re, sys, time

KEYS = ("source", "build", "start", "site")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def read(path):
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def stamp(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stages(path, status, now):
    out = [{"key": k, "status": "pending"} for k in KEYS]
    by_key = {s["key"]: s for s in out}
    seen = False
    for line in read(path).splitlines():
        parts = line.split(" ", 3)
        if len(parts) < 3 or not parts[0].isdigit() or parts[1] not in by_key or parts[2] not in ("running", "skipped"):
            continue
        seen = True
        ts, cur = stamp(int(parts[0])), by_key[parts[1]]
        note = " ".join(ANSI.sub("", parts[3] if len(parts) > 3 else "").split())[:200]
        # Начало этапа закрывает предыдущий: рецепты отмечают только начала.
        for s in out:
            if s is not cur and s["status"] == "running":
                s["status"], s["finished_at"] = "done", ts
        if parts[2] == "skipped":
            cur.update(status="skipped", note=note)
            continue
        if cur["status"] != "running":
            cur.update(status="running", started_at=ts)
        if note:
            cur["note"] = note
    if not seen:
        return None
    if status != "running":
        for s in out:
            if s["status"] == "running":
                s["status"], s["finished_at"] = ("done" if status == "success" else "failed"), stamp(now)
            elif s["status"] == "pending" and status == "success":
                s["status"] = "skipped"
    return out


mode, status, message, log_path, stages_path = sys.argv[1:6]
log = ANSI.sub("", read(log_path))
tail = log.encode()[-32768:].decode("utf-8", "ignore")
st = stages(stages_path, status if mode == "result" else "running", time.time())
if mode == "progress":
    print(json.dumps({"log": tail, "stages": st or []}, ensure_ascii=False))
    sys.exit(0)
if not message:
    prefix = "Ошибка:" if status == "failed" else "Готово:"
    for line in reversed(log.splitlines()):
        if line.startswith(prefix):
            message = line[len("Ошибка:"):].strip() if status == "failed" else line.strip()
            break
if not message and status == "failed":
    message = "команда завершилась с ошибкой, подробности в журнале"
body = {"status": status, "message": message[:2000], "log": tail}
if st is not None:
    body["stages"] = st
print(json.dumps(body, ensure_ascii=False))
'

# send_result ID STATUS СООБЩЕНИЕ [ЛОГ] [ЭТАПЫ] — итог важнее скорости: при сбое связи
# повторяем несколько раз, иначе дашборд будет час считать команду выполняющейся.
send_result() {
	local id=$1 status=$2 message=$3 log=${4:-} stages=${5:-} body=$RUN_DIR/result.json code i
	python3 -c "$PAYLOAD_PY" result "$status" "$message" "$log" "$stages" >"$body" || return
	for i in 1 2 3 4 5 6; do
		code=$(dashboard_fetch POST "/agent/v1/commands/${id}/result" /dev/null "@${body}") || code=000
		case $code in
		2??)
			echo "команда ${id}: ${status}"
			break
			;;
		400 | 404 | 413)
			echo "дашборд не принял итог команды ${id} (код ${code})"
			break
			;;
		esac
		sleep $((i * 10))
	done
	rm -f "$body"
}

# stream_progress ID ЛОГ ЭТАПЫ — пока идёт выкатка, шлёт дашборду хвост лога и этапы, когда
# они меняются: иначе человек минутами смотрит на «Выкатывается» без единой строки.
# 404 — старый дашборд без этого запроса или выкатка уже закрыта: дальше слать незачем.
stream_progress() {
	local id=$1 log=$2 stages=$3 body=$RUN_DIR/progress.json sent="" sum code
	while sleep "$PROGRESS_EVERY"; do
		python3 -c "$PAYLOAD_PY" progress running "" "$log" "$stages" >"$body" || continue
		sum=$(cksum <"$body")
		[[ $sum == "$sent" ]] && continue
		code=$(dashboard_fetch POST "/agent/v1/commands/${id}/log" /dev/null "@${body}") || code=000
		case $code in
		2??) sent=$sum ;;
		400 | 404 | 413) break ;;
		esac
	done
}

# open_env APP — расшифровать переменные из дашборда в файл на tmpfs. Ключ сервера только
# у агента: дашборд прислал шифротексты, открытых значений у него нет.
open_env() {
	local app=$1 err=$RUN_DIR/env.err
	install -m 600 /dev/null "$PANEL_ENV"
	if ! ensure_env_key; then
		echo "не удалось создать ключ сервера ${ENV_KEY}" >"$err"
		return 1
	fi
	python3 "$RECIPES_DIR/envkey.py" panel "$ENV_KEY" "$app" <"$ENV_JSON" >"$PANEL_ENV" 2>"$err"
}

run_deploy() {
	local id=$1 app=$2 repo=$3 sha=$4 ref=$5 root=$6 title=$7 token=$8 has_env=$9
	local log=$STATE_DIR/command.log stages=$STATE_DIR/command.stages public=0 rc message="" streamer panel=""
	[[ -z $token ]] && public=1
	echo "команда ${id}: выкатка ${repo}${root:+/${root}} ${sha:0:12} «${title}» как ${app}"
	if [[ $has_env == 1 ]]; then
		if ! open_env "$app"; then
			send_result "$id" failed "переменные окружения из дашборда: $(head -c 1000 "$RUN_DIR/env.err")"
			rm -f "$ENV_JSON" "$PANEL_ENV" "$RUN_DIR/env.err"
			return
		fi
		panel=$PANEL_ENV
	fi
	rm -f "$ENV_JSON" "$RUN_DIR/env.err"
	install -m 600 /dev/null "$log"
	install -m 600 /dev/null "$stages"
	stream_progress "$id" "$log" "$stages" &
	streamer=$!
	# Токен — только в окружении дочернего процесса: в аргументах его было бы видно в ps.
	AIRLOK_GITHUB_TOKEN=$token AIRLOK_GITHUB_PUBLIC=$public AIRLOK_FETCH_REF=$ref \
		AIRLOK_ROOT_DIR=$root AIRLOK_APP_NAME=$app AIRLOK_STAGE_FILE=$stages AIRLOK_BUILD_OUTPUT=1 \
		AIRLOK_PANEL_ENV=$panel \
		timeout --kill-after=60 "$DEPLOY_TIMEOUT" "$RECIPES_DIR/github-deploy.sh" deploy "$repo" "$sha" \
		</dev/null >"$log" 2>&1
	rc=$?
	kill "$streamer" 2>/dev/null
	wait "$streamer" 2>/dev/null
	rm -f "$RUN_DIR/progress.json" "$PANEL_ENV"
	if ((rc == 124 || rc == 137)); then
		message="выкатка не уложилась в $((DEPLOY_TIMEOUT / 60)) минут и была остановлена"
	fi
	if ((rc == 0)); then
		send_result "$id" success "$message" "$log" "$stages"
	else
		send_result "$id" failed "$message" "$log" "$stages"
	fi
}

# run_update — обновление агента, которое человек подтвердил в дашборде. После отчёта агент
# перезапускается: так он продолжит на новом коде, даже если сам agent.sh не менялся.
run_update() {
	local id=$1 log=$STATE_DIR/command.log rc message=""
	echo "команда ${id}: обновление агента"
	install -m 600 /dev/null "$log"
	timeout --kill-after=60 "$UPDATE_TIMEOUT" "$RECIPES_DIR/update.sh" </dev/null >"$log" 2>&1
	rc=$?
	if ((rc == 124 || rc == 137)); then
		message="обновление не уложилось в $((UPDATE_TIMEOUT / 60)) минут и было остановлено"
	fi
	if ((rc != 0)); then
		send_result "$id" failed "$message" "$log"
		return
	fi
	send_result "$id" success "$message" "$log"
	if [[ -e $AGENT_RESTART_FLAG ]]; then
		rm -f "$AGENT_RESTART_FLAG"
		echo "служба агента изменилась, перезапуск"
		systemctl restart --no-block airlok-agent.service
		# Ждём, пока systemd остановит этот процесс: старый код не должен взять новую команду.
		sleep 60
		return
	fi
	echo "агент обновлён, перезапуск на новом коде"
	exec "$SELF"
}

handle() {
	local f=()
	rm -f "$ENV_JSON"
	mapfile -d '' -t f < <(python3 -c "$PARSE_PY" "$ENV_JSON" <"$1")
	rm -f "$1"
	if [[ ${f[0]:-} != ok ]]; then
		echo "команда ${f[1]:-без id} отклонена: ${f[2]:-ответ дашборда не разобрать}"
		[[ -n ${f[1]:-} ]] && send_result "${f[1]}" failed "${f[2]:-}"
		return
	fi
	case ${f[2]:-} in
	update) run_update "${f[1]}" ;;
	*) run_deploy "${f[1]}" "${f[@]:3}" ;;
	esac
}

main() {
	need_root
	install -d -m 700 "$RUN_DIR" "$STATE_DIR"
	local backoff=5 code started resp=$RUN_DIR/next.json
	echo "агент запущен, версия $(agent_version)"
	if ensure_env_key; then
		echo "ключ переменных окружения: $(env_key_id) — тот же отпечаток показывает дашборд"
	else
		echo "не удалось создать ключ ${ENV_KEY}: переменные из дашборда не расшифровать"
	fi
	while :; do
		# git pull заменяет файл новым: между командами перезапускаемся на свежем коде.
		if [[ $(stat -c %i "$SELF" 2>/dev/null) != "$SELF_INODE" ]]; then
			echo "рецепты обновились, перезапуск"
			exec "$SELF"
		fi
		if [[ ! -r $DASHBOARD_CONF ]]; then
			sleep 30
			continue
		fi
		started=$SECONDS
		code=$(dashboard_fetch GET "/agent/v1/commands/next?wait=${WAIT}" "$resp" "" $((WAIT + 20))) || code=000
		case $code in
		200)
			backoff=5
			handle "$resp"
			;;
		204)
			backoff=5
			# Дашборд, который не держит запрос, отвечает сразу — не засыпаем его запросами.
			((SECONDS - started < 2)) && sleep 5
			;;
		401 | 403)
			echo "дашборд не принимает токен сервера, следующая попытка через 5 минут"
			sleep 300
			;;
		404)
			# Дашборд без команд для агента: проверим, не обновился ли он.
			sleep 300
			;;
		*)
			echo "дашборд недоступен (код ${code}), повтор через ${backoff} с"
			sleep "$backoff"
			backoff=$((backoff * 2 > 300 ? 300 : backoff * 2))
			;;
		esac
		rm -f "$resp"
	done
}

main "$@"
