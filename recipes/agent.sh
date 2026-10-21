#!/usr/bin/env bash
# Агент: забирает команды из дашборда долгим опросом и выполняет их по одной.
#
# Работает службой airlok-agent от root, потому что деплою нужны его права. Умеет только
# выкатить коммит из GitHub: произвольную команду дашборд прислать не может, а каждое
# поле команды проверяется здесь заново — значения доходят до команд от root.
# Сервер сам ходит в дашборд, входящих соединений агент не принимает.

source "$(dirname "$0")/lib.sh"
set +e

WAIT=25
# tmpfs: в ответе дашборда токен GitHub, на диск он попадать не должен.
RUN_DIR=/run/airlok-agent
STATE_DIR=/var/lib/airlok/agent
DEPLOY_TIMEOUT=3600

SELF=$(readlink -f "$0")
SELF_INODE=$(stat -c %i "$SELF" 2>/dev/null)

# Разбор команды. Поля выводятся через NUL в фиксированном порядке и в shell не
# исполняются; первое поле — ok или bad, второе — id команды (если его удалось понять).
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
emit("ok", cid, app, repo, sha, ref, root, " ".join(title.split())[:200], token)
'

# Итог команды в JSON: сообщение — строка «Ошибка: …» или «Готово: …» из вывода,
# лог — хвост вывода, потому что причина неудачи обычно в последних строках.
RESULT_PY='
import json, re, sys

status, message, path = sys.argv[1:4]
log = ""
if path:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            log = f.read()
    except OSError:
        pass
log = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", log)
if not message:
    prefix = "Ошибка:" if status == "failed" else "Готово:"
    for line in reversed(log.splitlines()):
        if line.startswith(prefix):
            message = line[len("Ошибка:"):].strip() if status == "failed" else line.strip()
            break
if not message and status == "failed":
    message = "выкатка завершилась с ошибкой, подробности в журнале"
tail = log.encode()[-32768:].decode("utf-8", "ignore")
print(json.dumps({"status": status, "message": message[:2000], "log": tail}, ensure_ascii=False))
'

# send_result ID STATUS СООБЩЕНИЕ [ЛОГ] — итог важнее скорости: при сбое связи
# повторяем несколько раз, иначе дашборд будет час считать команду выполняющейся.
send_result() {
	local id=$1 status=$2 message=$3 log=${4:-} body=$RUN_DIR/result.json code i
	python3 -c "$RESULT_PY" "$status" "$message" "$log" >"$body" || return
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

run_deploy() {
	local id=$1 app=$2 repo=$3 sha=$4 ref=$5 root=$6 title=$7 token=$8
	local log=$STATE_DIR/command.log public=0 rc message=""
	[[ -z $token ]] && public=1
	echo "команда ${id}: выкатка ${repo}${root:+/${root}} ${sha:0:12} «${title}» как ${app}"
	install -m 600 /dev/null "$log"
	# Токен — только в окружении дочернего процесса: в аргументах его было бы видно в ps.
	AIRLOK_GITHUB_TOKEN=$token AIRLOK_GITHUB_PUBLIC=$public AIRLOK_FETCH_REF=$ref \
		AIRLOK_ROOT_DIR=$root AIRLOK_APP_NAME=$app \
		timeout --kill-after=60 "$DEPLOY_TIMEOUT" "$RECIPES_DIR/github-deploy.sh" deploy "$repo" "$sha" \
		</dev/null >"$log" 2>&1
	rc=$?
	if ((rc == 124 || rc == 137)); then
		message="выкатка не уложилась в $((DEPLOY_TIMEOUT / 60)) минут и была остановлена"
	fi
	if ((rc == 0)); then
		send_result "$id" success "$message" "$log"
	else
		send_result "$id" failed "$message" "$log"
	fi
}

handle() {
	local f=()
	mapfile -d '' -t f < <(python3 -c "$PARSE_PY" <"$1")
	rm -f "$1"
	if [[ ${f[0]:-} != ok ]]; then
		echo "команда ${f[1]:-без id} отклонена: ${f[2]:-ответ дашборда не разобрать}"
		[[ -n ${f[1]:-} ]] && send_result "${f[1]}" failed "${f[2]:-}"
		return
	fi
	run_deploy "${f[@]:1}"
}

main() {
	need_root
	install -d -m 700 "$RUN_DIR" "$STATE_DIR"
	local backoff=5 code started resp=$RUN_DIR/next.json
	echo "агент запущен, версия $(agent_version)"
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
