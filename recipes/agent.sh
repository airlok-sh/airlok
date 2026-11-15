#!/usr/bin/env bash
# Агент: забирает команды из дашборда долгим опросом и выполняет их по одной.
#
# Работает службой airlok-agent от root, потому что деплою нужны его права. Умеет только
# выкатить коммит из GitHub, перезапустить выкаченное приложение, убрать его с сервера,
# прочитать данные его базы (только чтение, через dbquery.py), положить root'у SSH-ключи
# устройств (sshkeys.py), закрыть или открыть вход по паролю, выгрузить и загрузить данные
# приложения при переезде, сделать копию базы в хранилище и восстановить её оттуда (app-data.sh),
# а по подтверждению человека в дашборде — обновиться сам, поставить обновления системы и
# перезагрузить сервер (os-upgrade.sh):
# произвольную команду дашборд прислать не может, а каждое поле команды проверяется здесь
# заново — значения доходят до команд от root.
# Сервер сам ходит в дашборд, входящих соединений агент не принимает.
#
# Обновление и ключи SSH всё же дают взломанному дашборду полный доступ к серверу. Строгий режим
# (strict.py) это закрывает: что дашборду нельзя, решает файл на сервере, а каждая команда пишется в
# журнал AUDIT_LOG, который дашборд не читает и стереть не может.

source "$(dirname "$0")/lib.sh"
set +e

WAIT=25
# tmpfs: в ответе дашборда токен GitHub, на диск он попадать не должен.
RUN_DIR=/run/airlok-agent
STATE_DIR=/var/lib/airlok/agent
DEPLOY_TIMEOUT=3600
UPDATE_TIMEOUT=1800
RESTART_TIMEOUT=600
REMOVE_TIMEOUT=600
DBQUERY_TIMEOUT=30
SSHKEYS_TIMEOUT=30
DATA_TIMEOUT=3600
# Столько ждём, пока человек войдёт на сервер по ключу после закрытия пароля; откат — чуть позже.
SSH_LOGIN_WAIT=300
# Как часто во время этого ожидания спрашивать новые ключи устройств.
SSH_LOGIN_SLICE=6
PROGRESS_EVERY=3
OS_UPGRADE_TIMEOUT=2700
# За столько сервер точно ушёл в перезагрузку, если собирался: дальше — итог «не перезагрузился».
OS_REBOOT_WAIT=180
OS_UNIT=airlok-os-upgrade
# Обновление системы может перезапустить самого агента, а перезагрузка — точно: что он выполняет,
# лежит на диске, и новый запуск дождётся конца и отправит итог.
OS_JOB=$STATE_DIR/os-upgrade.job
OS_RC=$STATE_DIR/os-upgrade.rc
OS_LOG=$STATE_DIR/os-upgrade.log
OS_STATUS=$STATE_DIR/os-upgrade.status
OS_APPS=$STATE_DIR/os-upgrade.apps
# Шифротексты из команды и расшифрованные значения — только на tmpfs и только на время выкатки.
ENV_JSON=$RUN_DIR/env.json
PANEL_ENV=$RUN_DIR/panel.env
# Запрос к базе из команды: в нём бывают данные из базы, поэтому тоже только tmpfs.
DBQ_JSON=$RUN_DIR/dbquery.json
KEYS_JSON=$RUN_DIR/sshkeys.json
ENV_ERR=$RUN_DIR/env.err
CMD_LOG=$STATE_DIR/command.log
# Долгая команда (публикация, перенос и копии данных) идёт в фоне, а агент тем временем берёт быстрые:
# запрос к базе, ключи устройств, вход по паролю, перезапуск и удаление других приложений. У неё свои
# файлы переменных, а у быстрых — свой журнал: иначе быстрый перезапуск затёр бы их посреди сборки.
LONG_ENV_JSON=$RUN_DIR/long-env.json
LONG_PANEL_ENV=$RUN_DIR/long-panel.env
LONG_ENV_ERR=$RUN_DIR/long-env.err
SIDE_LOG=$STATE_DIR/side-command.log
QUICK_KINDS=db_query,ssh_keys,ssh_password,restart,remove
# Опрос посреди долгой команды короче: её конец агент замечает только между опросами.
SIDE_WAIT=10
# Как часто снова пробовать опрос со списком видов, если дашборд его не знал: его могли обновить.
SIDE_RECHECK=600
LONG_PID="" LONG_APP="" LONG_ID="" LONG_KIND="" LONG_CHILD=""
# Столько остановленному рецепту долгой команды на выход, потом KILL всей его группе: systemd ждёт
# остановки службы 90 секунд (TimeoutStopSec по умолчанию), а итог «прервано» должен успеть лечь в
# очередь и уйти. На TERM рецепт под timeout из uutils (Ubuntu 26.04) может и не выйти.
STOP_GRACE=10
STOP_MESSAGE="прервано: служба агента остановилась (перезапуск агента или сервера). Запустите ещё раз"
# Быстрая команда, которую выполняет сейчас основной цикл: остановка службы и её не должна оставить без итога.
CUR_ID="" CUR_KIND="" CUR_APP="" CUR_LOG=""
# Команды, чей рецепт идёт своей службой systemd (detach в lib.sh) и пережил остановку агента: итог
# отправит агент после перезапуска, когда служба закончит. Строка — id, вид, служба, журнал.
RESUME=$STATE_DIR/resume
# list — опрос со списком быстрых видов, keys — только ключи устройств (дашборд не знает списка),
# none — посреди долгой команды не опрашиваем вовсе (не знает и этого).
SIDE_POLL=list SIDE_POLL_AT=0
BACKOFF=5
AUDIT_LOG=/var/log/airlok-commands.log
AUDIT_MAX_BYTES=$((5 * 1024 * 1024))
# Недоставленные итоги команд. В хвосте лога бывает вывод приложения с паролями баз, поэтому
# очередь на tmpfs, а не в RUN_DIR: его systemd стирает при каждом перезапуске службы.
RESULTS_DIR=/run/airlok-results
# Позже дашборд уже сам закрыл команду как «сервер не сообщил итог», и повтор ничего не изменит.
RESULT_KEEP=3600
# Выкатку без единого хода дашборд при следующем опросе вернёт в очередь как потерянную, поэтому
# с недоставленным итогом выкатки новые команды не берём — но не дольше этого.
RESULT_HOLD=600
RESULT_RETRY=30
MAX_RESULTS=20
# Возраст итогов — по времени с загрузки: стенные часы после загрузки бывает переводит NTP, а
# очередь на tmpfs загрузку всё равно не переживает.
UPTIME=/proc/uptime

SELF=$(readlink -f "$0")
SELF_INODE=$(stat -c %i "$SELF" 2>/dev/null)

# Разбор команды. Поля выводятся через NUL в фиксированном порядке и в shell не
# исполняются; первое поле — ok или bad, второе — id команды (если его удалось понять),
# третье у ok — вид команды. Зашифрованные переменные из дашборда пишутся в файл из
# первого аргумента, последнее поле выкатки и перезапуска — 1, если они пришли. Запрос к базе
# пишется во второй, ключи устройств — в третий: их поля целиком проверяют dbquery.py и sshkeys.py.
PARSE_PY='
import json, re, sys

UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
# Временная ссылка хранилища: только https и без пробелов и кавычек; в shell она не попадает.
STORE_URL = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/[^\s\"\x27\\]{1,8192}$")
REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
REF = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]*$")
# Сегмент каталога не начинается с точки или дефиса: так отсекаются «..», скрытые
# каталоги и значения, похожие на опции.
SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
# Токены установки GitHub App бывают и короткими ghs_…, и длинными, с точками (около 400 символов).
TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,2048}$")
# Код по ссылке (GitVerse, GitFlic и другие): дашборд присылает её в виде https://хост/путь.git,
# а доступ — готовым значением Basic (base64).
GIT_URL = re.compile(r"^https://[a-z0-9.-]+(/[A-Za-z0-9_][A-Za-z0-9_.~-]*){2,8}\.git$")
GIT_AUTH = re.compile(r"^[A-Za-z0-9+/=]{1,4096}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# Домен из дашборда — ASCII, кириллица уже в punycode (xn--p1ai для .рф).
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63}|xn--[a-z0-9-]{1,59})$")
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


# save_env — переменные из команды в файл; None — их нет в команде, файл приложения не трогаем.
def save_env(env):
    if env is None:
        return "0"
    if not isinstance(env, list) or len(env) > 100 or not all(
            isinstance(v, dict) and isinstance(v.get("name"), str) and ENV_NAME.match(v["name"])
            and isinstance(v.get("value"), str) and SEALED.match(v["value"]) for v in env):
        emit("bad", cid, "некорректные переменные окружения в команде")
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        json.dump([{"name": v["name"], "value": v["value"]} for v in env], f)
    return "1"


kind = cmd.get("kind")
# Хеш версии необязателен: без него ставится последняя подписанная. Что коммит из main и подписан, проверяет
# update.sh, а хеш не того вида просто не передаётся.
if kind == "update":
    u = cmd.get("update")
    sha = u.get("sha") if isinstance(u, dict) else None
    if isinstance(sha, str) and SHA.fullmatch(sha):
        emit("ok", cid, "update", sha)
    emit("ok", cid, "update")
if kind == "db_query":
    q = cmd.get("db_query")
    if not isinstance(q, dict):
        emit("bad", cid, "в команде нет запроса к базе")
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump(q, f)
    emit("ok", cid, "db_query")
# ssh_keys_remove — те же ключи плюс чужие, которые надо убрать: агент, не знающий этой команды,
# откажется от неё и не отчитается об успехе, ничего не убрав.
if kind in ("ssh_keys", "ssh_keys_remove"):
    k = cmd.get("ssh_keys")
    if not isinstance(k, dict):
        emit("bad", cid, "в команде нет ключей устройств")
    with open(sys.argv[3], "w", encoding="utf-8") as f:
        json.dump(k, f)
    emit("ok", cid, "ssh_keys")
if kind == "ssh_password":
    p = cmd.get("ssh_password")
    if not isinstance(p, dict) or p.get("login") not in ("closed", "open"):
        emit("bad", cid, "в команде нет нужного входа по паролю: closed или open")
    # Пользователь, которому лежат ключи устройств: его ключ и проверяется перед закрытием.
    user = p.get("user", "root")
    if not isinstance(user, str) or not USER.match(user):
        emit("bad", cid, "некорректный пользователь сервера")
    emit("ok", cid, "ssh_password", p["login"], user)
if kind == "os_upgrade":
    u = cmd.get("os_upgrade")
    if not isinstance(u, dict) or u.get("action") not in ("upgrade", "reboot"):
        emit("bad", cid, "в команде нет нужного действия с системой: upgrade или reboot")
    emit("ok", cid, "os_upgrade", u["action"])
if kind == "data_move":
    m = cmd.get("data_move")
    if not isinstance(m, dict) or m.get("op") not in ("export", "import", "resume", "backup", "restore"):
        emit("bad", cid, "в команде нет нужного действия с данными: export, import, resume, backup или restore")
    app = m.get("app")
    if not isinstance(app, str) or not NAME.match(app):
        emit("bad", cid, "некорректное имя приложения")
    urls = [m.get(k) or "" for k in ("db_url", "files_url", "data_url")]
    if not all(isinstance(u, str) and (u == "" or STORE_URL.fullmatch(u)) for u in urls):
        emit("bad", cid, "некорректная ссылка на хранилище")
    if m["op"] == "backup" and not urls[0]:
        emit("bad", cid, "нет ссылки на копию базы в хранилище")
    if m["op"] == "restore" and not urls[0] and not urls[2]:
        emit("bad", cid, "нет ссылки на копию в хранилище")
    emit("ok", cid, "data_move", m["op"], app, *urls)
if kind == "restart":
    r = cmd.get("restart")
    if not isinstance(r, dict):
        emit("bad", cid, "в команде нет параметров перезапуска")
    app = r.get("app")
    if not isinstance(app, str) or not NAME.match(app):
        emit("bad", cid, "некорректное имя приложения")
    emit("ok", cid, "restart", app, save_env(r.get("env")))
if kind == "remove":
    r = cmd.get("remove")
    if not isinstance(r, dict):
        emit("bad", cid, "в команде нет параметров удаления")
    app, with_data = r.get("app"), r.get("with_data", False)
    if not isinstance(app, str) or not NAME.match(app):
        emit("bad", cid, "некорректное имя приложения")
    # Только настоящий bool: строка "false" не должна удалить данные.
    if not isinstance(with_data, bool):
        emit("bad", cid, "поле with_data должно быть true или false")
    emit("ok", cid, "remove", app, "1" if with_data else "0")
if kind != "deploy":
    emit("bad", cid, "агент не умеет выполнять команду %r: обновите рецепты на сервере" % str(kind)[:40])
d = cmd.get("deploy")
if not isinstance(d, dict):
    emit("bad", cid, "в команде нет параметров публикации")


def text(key):
    v = d.get(key)
    if v is None:
        return ""
    if not isinstance(v, str):
        emit("bad", cid, "поле %s должно быть строкой" % key)
    return v


app, repo, sha, ref, root, title, token, domain, git_auth = (
    text(k) for k in ("app", "repo", "sha", "ref", "root_dir", "title", "github_token", "domain", "git_auth"))
if not NAME.match(app):
    emit("bad", cid, "некорректное имя приложения")
if repo.startswith("https://"):
    if not GIT_URL.fullmatch(repo) or ".." in repo:
        emit("bad", cid, "некорректная ссылка на репозиторий")
else:
    parts = repo.split("/")
    if len(parts) != 2 or not all(REPO_PART.match(p) and p not in (".", "..") for p in parts):
        emit("bad", cid, "некорректный репозиторий")
if git_auth and not GIT_AUTH.fullmatch(git_auth):
    emit("bad", cid, "некорректный доступ к репозиторию")
if not SHA.match(sha):
    emit("bad", cid, "некорректный коммит")
if ref and (not REF.match(ref) or ".." in ref):
    emit("bad", cid, "некорректная ветка")
if root and not all(SEGMENT.match(s) for s in root.split("/")):
    emit("bad", cid, "некорректный каталог в репозитории")
if token and not TOKEN.match(token):
    emit("bad", cid, "некорректный токен GitHub")
# fullmatch: у match знак $ пропускает перевод строки в конце.
if domain and not DOMAIN.fullmatch(domain):
    emit("bad", cid, "некорректный домен")
# Нет поля — у дашборда нет ключа сервера или он старый: файл переменных не трогаем.
has_env = save_env(d.get("env"))
emit("ok", cid, "deploy", app, repo, sha, ref, root, " ".join(title.split())[:200], token, domain, has_env, git_auth)
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


# apt_percent — общий прогресс по строкам APT::Status-Fd: скачивание идёт по одной
# шкале, установка по другой, а по времени установка заметно дольше. 99 — конец приходит с итогом.
def apt_percent(path):
    for line in reversed(read(path).splitlines()):
        parts = line.split(":", 3)
        if len(parts) < 3 or parts[0] not in ("dlstatus", "pmstatus"):
            continue
        try:
            value = float(parts[2])
        except ValueError:
            continue
        return min(99, max(0, int(value * 0.3 if parts[0] == "dlstatus" else 30 + value * 0.7)))
    return 0


mode, status, message, log_path, stages_path = sys.argv[1:6]
title_path = sys.argv[6] if len(sys.argv) > 6 else ""
status_path = sys.argv[7] if len(sys.argv) > 7 else ""
log = ANSI.sub("", read(log_path))
tail = log.encode()[-32768:].decode("utf-8", "ignore")
st = stages(stages_path, status if mode == "result" else "running", time.time())
if mode == "progress":
    body = {"log": tail, "stages": st or []}
    if status_path:
        body["percent"] = apt_percent(status_path)
    print(json.dumps(body, ensure_ascii=False))
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
# Заголовок коммита из кода по ссылке: дашборд без API площадки его не знает.
title = " ".join(read(title_path).split())[:500]
if title:
    body["title"] = title
print(json.dumps(body, ensure_ascii=False))
'

# jitter СЕКУНДЫ — пауза со случайной добавкой до половины: после перезапуска дашборда агенты
# не должны повторять запросы в одни и те же секунды.
jitter() { echo $(($1 + RANDOM % ($1 / 2 + 1))); }

# send_result ID STATUS СООБЩЕНИЕ [ЛОГ] [ЭТАПЫ] [ЗАГОЛОВОК] — итог ложится в очередь и сразу
# отправляется. Не дошёл — его дошлёт основной цикл, а агент тем временем берёт новые команды.
# Дашборд принимает повтор итога без последствий: закрытая команда второй раз не закрывается.
send_result() {
	local id=$1 status=$2 message=$3 log=${4:-} stages=${5:-} title=${6:-} name up
	# Службу остановили посреди долгой команды: рецепт оборван, и причина не в проекте.
	if [[ ${LONG_STOP:-} == 1 && $status == failed ]]; then
		message=$STOP_MESSAGE
	fi
	# TERM от systemd при остановке службы мог оборвать и шаг сохранения, если совпал с концом команды:
	# второй раз systemd его не шлёт, поэтому оборванный шаг повторяем.
	up=$(uptime_cs) || up=$(uptime_cs)
	# Имя начинается с сотых секунды с загрузки одной ширины: сортировка по имени — порядок итогов.
	name=$(printf '%015d' "$up")-${id}-${status}
	# Вид команды неизвестен, если её не удалось разобрать: это могла быть и выкатка.
	[[ ${CMD_KIND:-} == deploy || ${CMD_KIND:-} == unknown ]] && name+=-hold
	if ! result_file "$name" "$status" "$message" "$log" "$stages" "$title" &&
		! { [[ ${LONG_STOP:-} == 1 ]] && result_file "$name" "$status" "$message" "$log" "$stages" "$title"; }; then
		echo "итог команды ${id} (${status}) не удалось сохранить в ${RESULTS_DIR} (кончилось место?), в дашборд он не уйдёт"
		return
	fi
	printf '%s\n' "$id" >>"$RUN_DIR/sent" 2>/dev/null
	flush_results
	[[ -e $RESULTS_DIR/${name}.json ]] && echo "итог команды ${id} (${status}) пока не доставлен, дошлём позже"
	return 0
}

# result_file ИМЯ СТАТУС СООБЩЕНИЕ ЛОГ ЭТАПЫ ЗАГОЛОВОК — итог в очередь. Через временный файл: отправка не
# должна подхватить недописанный.
result_file() {
	local tmp=$RESULTS_DIR/.$1.tmp
	install -d -m 700 "$RESULTS_DIR" && python3 -c "$PAYLOAD_PY" result "${@:2}" >"$tmp" && mv "$tmp" "$RESULTS_DIR/$1.json" && return 0
	rm -f "$tmp"
	return 1
}

results_pending() { compgen -G "$RESULTS_DIR/*.json" >/dev/null; }

# sent_already ID — итог команды уже лёг в очередь: при остановке второй, «прервано», не нужен.
sent_already() { grep -qxF "$1" "$RUN_DIR/sent" 2>/dev/null; }

# job_pending ID СЛУЖБА — итог команды ID знает её служба, а не остановленный агент: она ещё работает или
# успела закончить сама, пока агент останавливался, — тогда «прервано» было бы неправдой. Служба другой
# команды (прошлая публикация, очередь webhook) не в счёт: эта до своей службы не дошла.
job_pending() {
	[[ -n $1 && $(cat "$JOBS_DIR/$2.owner" 2>/dev/null) == "$1" ]] || return 1
	systemctl is-active --quiet "$2" 2>/dev/null ||
		[[ $(sed -n 1p "$JOBS_DIR/$2.outcome" 2>/dev/null) =~ ^(success|failed)$ ]]
}

# resume_later ID ВИД СЛУЖБА ЖУРНАЛ — итог отправит агент после перезапуска (resume_jobs).
resume_later() {
	printf '%s %s %s %s\n' "$@" >>"$RESUME"
	printf '%s\n' "$1" >>"$RUN_DIR/sent" 2>/dev/null
	echo "команда ${1}: итог у службы ${3}, он придёт после перезапуска агента"
}

# resume_jobs — агент запустился, а команды, пережившие его остановку, без итога: дождаться их служб и
# отправить настоящий итог. Служба кончилась, не оставив итога, — её оборвали.
resume_jobs() {
	[[ -s $RESUME ]] || return 0
	local id kind unit log status outcome stages title limit
	while read -r id kind unit log; do
		[[ $id =~ ^[0-9a-fA-F-]{36}$ && $unit =~ ^airlok-(publish|restart)-[a-z][a-z0-9-]{1,30}$ ]] || continue
		echo "команда ${id}: ждём, пока ${unit} закончит"
		limit=$((SECONDS + DEPLOY_TIMEOUT))
		while systemctl is-active --quiet "$unit" && ((SECONDS < limit)); do
			sleep 2
		done
		outcome=$JOBS_DIR/${unit}.outcome
		status=$(sed -n 1p "$outcome" 2>/dev/null)
		stages="" title=""
		[[ $kind == deploy ]] && stages=$STATE_DIR/command.stages title=$STATE_DIR/command.title
		case $status in
		success) CMD_KIND=$kind send_result "$id" success "" "$log" "$stages" "$title" ;;
		failed) CMD_KIND=$kind send_result "$id" failed "" "$log" "$stages" "$title" ;;
		*) CMD_KIND=$kind send_result "$id" failed "$STOP_MESSAGE" "$log" "$stages" "$title" ;;
		esac
		rm -f "$outcome"
	done <"$RESUME"
	rm -f "$RESUME"
}
# uptime_cs — сотые секунды с загрузки.
uptime_cs() {
	local up
	read -r up _ <"$UPTIME" 2>/dev/null && [[ $up =~ ^[0-9]+\.[0-9]+$ ]] || return 1
	echo $((10#${up/./}))
}

# result_age ИМЯ_ФАЙЛА — сколько секунд итог в очереди; непонятное имя — заведомо старый итог.
result_age() {
	local at=${1%%-*} now
	now=$(uptime_cs) || { echo 999999; return; }
	if [[ $at =~ ^[0-9]{1,15}$ ]] && ((10#$at <= now)); then
		echo $(((now - 10#$at) / 100))
	else
		echo 999999
	fi
}

# drop_result ID — забыть недоставленный итог: дашборд выдал команду заново, и прежний итог закрыл
# бы уже новый её запуск. Под тем же замком, что и отправка, чтобы не уйти посреди неё.
drop_result() {
	[[ -d $RESULTS_DIR ]] || return 0
	(
		flock -w 30 9 || true
		shopt -s nullglob
		local f
		for f in "$RESULTS_DIR"/*-"$1"-*.json; do
			echo "команда ${1} пришла снова: прежний недоставленный итог больше не отправляем"
			rm -f "$f"
		done
	) 9>"$RESULTS_DIR/.lock"
}

# flush_results — дослать итоги по порядку. На первой временной ошибке останавливается: дашборд
# недоступен, и остальные подождут следующей попытки.
flush_results() {
	results_pending || return 0
	(
		# Отправляет кто-то один, иначе один итог уйдёт дважды.
		flock -n 9 || exit 0
		shopt -s nullglob
		local files=("$RESULTS_DIR"/*.json) f base id status code
		if ((${#files[@]} > MAX_RESULTS)); then
			rm -f "${files[@]:0:${#files[@]}-MAX_RESULTS}"
			files=("${files[@]:${#files[@]}-MAX_RESULTS}")
		fi
		for f in "${files[@]}"; do
			base=${f##*/}
			base=${base%.json}
			base=${base%-hold}
			status=${base##*-}
			id=${base#*-}
			id=${id%-*}
			if [[ ! $id =~ ^[0-9a-fA-F-]{36}$ ]]; then
				rm -f "$f"
				continue
			fi
			if (($(result_age "$base") > RESULT_KEEP)); then
				echo "итог команды ${id} (${status}) не доставлен за $((RESULT_KEEP / 60)) минут, больше не отправляем"
				rm -f "$f"
				continue
			fi
			code=$(dashboard_fetch POST "/agent/v1/commands/${id}/result" /dev/null "@${f}") || code=000
			case $code in
			2??)
				echo "команда ${id}: ${status}"
				rm -f "$f"
				;;
			400 | 404 | 413)
				echo "дашборд не принял итог команды ${id} (код ${code})"
				rm -f "$f"
				;;
			*) break ;;
			esac
		done
	) 9>"$RESULTS_DIR/.lock"
}

# results_hold — есть недоставленный итог выкатки моложе RESULT_HOLD: новую команду брать рано.
results_hold() (
	shopt -s nullglob
	local f
	for f in "$RESULTS_DIR"/*-hold.json; do
		(($(result_age "${f##*/}") < RESULT_HOLD)) && exit 0
	done
	exit 1
)

# results_flusher — пока идёт долгая команда, досылать итоги прежних: перезапуск или ключи
# дашборд закрывает как «сервер не сообщил итог» уже через 10–15 минут.
results_flusher() {
	while sleep "$(jitter "$RESULT_RETRY")"; do
		flush_results
		results_pending || return 0
	done
}

# stream_progress ID ЛОГ ЭТАПЫ — пока идёт выкатка, шлёт дашборду хвост лога и этапы, когда
# они меняются: иначе человек минутами смотрит на «Выкатывается» без единой строки.
# 404 — старый дашборд без этого запроса или выкатка уже закрыта: дальше слать незачем.
stream_progress() {
	local id=$1 log=$2 stages=$3 apt_status=${4:-} body=$RUN_DIR/progress.json sent="" sum code
	while sleep "$PROGRESS_EVERY"; do
		python3 -c "$PAYLOAD_PY" progress running "" "$log" "$stages" "" "$apt_status" >"$body" || continue
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
	local app=$1 err=$ENV_ERR
	install -m 600 /dev/null "$PANEL_ENV"
	if ! ensure_env_key; then
		echo "не удалось создать ключ сервера ${ENV_KEY}" >"$err"
		return 1
	fi
	python3 "$RECIPES_DIR/envkey.py" panel "$ENV_KEY" "$app" <"$ENV_JSON" >"$PANEL_ENV" 2>"$err"
}

run_deploy() {
	local id=$1 app=$2 repo=$3 sha=$4 ref=$5 root=$6 title=$7 token=$8 domain=$9 has_env=${10} git_auth=${11:-}
	local log=$CMD_LOG stages=$STATE_DIR/command.stages subject=$STATE_DIR/command.title public=0 rc message="" streamer panel=""
	[[ -z $token ]] && public=1
	echo "команда ${id}: публикация ${repo}${root:+/${root}} ${sha:0:12} «${title}» как ${app}${domain:+ на ${domain}}"
	if [[ $has_env == 1 ]]; then
		if ! open_env "$app"; then
			send_result "$id" failed "переменные окружения из дашборда: $(head -c 1000 "$ENV_ERR")"
			rm -f "$ENV_JSON" "$PANEL_ENV" "$ENV_ERR"
			return
		fi
		panel=$PANEL_ENV
	fi
	rm -f "$ENV_JSON" "$ENV_ERR"
	install -m 600 /dev/null "$log"
	install -m 600 /dev/null "$stages"
	install -m 600 /dev/null "$subject"
	stream_progress "$id" "$log" "$stages" &
	streamer=$!
	# Токен — только в окружении дочернего процесса: в аргументах его было бы видно в ps.
	AIRLOK_JOB_ID=$id AIRLOK_GITHUB_TOKEN=$token AIRLOK_GITHUB_PUBLIC=$public AIRLOK_GIT_AUTH=$git_auth AIRLOK_FETCH_REF=$ref \
		AIRLOK_ROOT_DIR=$root AIRLOK_APP_NAME=$app AIRLOK_STAGE_FILE=$stages AIRLOK_BUILD_OUTPUT=1 \
		AIRLOK_PANEL_ENV=$panel AIRLOK_APP_DOMAIN=$domain AIRLOK_TITLE_FILE=$subject \
		timeout --kill-after=60 "$DEPLOY_TIMEOUT" "$RECIPES_DIR/github-deploy.sh" deploy "$repo" "$sha" \
		</dev/null >"$log" 2>&1 &
	recipe_wait $!
	rc=$?
	kill "$streamer" 2>/dev/null
	wait "$streamer" 2>/dev/null
	rm -f "$RUN_DIR/progress.json" "$PANEL_ENV"
	if ((rc == 124 || rc == 137)); then
		message="публикация не уложилась в $((DEPLOY_TIMEOUT / 60)) минут и была остановлена"
	fi
	# Службу агента остановили, когда публикация уже меняла версию: её служба доводит дело до конца.
	if [[ ${LONG_STOP:-} == 1 ]] && job_pending "$id" "airlok-publish-${app}"; then
		resume_later "$id" deploy "airlok-publish-${app}" "$log"
		return
	fi
	if ((rc == 0)); then
		send_result "$id" success "$message" "$log" "$stages" "$subject"
	else
		send_result "$id" failed "$message" "$log" "$stages" "$subject"
	fi
	rm -f "$subject"
}

# run_restart — перезапуск выкаченного приложения без сборки: с переменными из дашборда, если
# они пришли.
run_restart() {
	local id=$1 app=$2 has_env=$3 log=$CMD_LOG rc message="" panel=""
	echo "команда ${id}: перезапуск ${app}"
	if [[ $has_env == 1 ]]; then
		if ! open_env "$app"; then
			send_result "$id" failed "переменные окружения из дашборда: $(head -c 1000 "$ENV_ERR")"
			rm -f "$ENV_JSON" "$PANEL_ENV" "$ENV_ERR"
			return
		fi
		panel=$PANEL_ENV
	fi
	rm -f "$ENV_JSON" "$ENV_ERR"
	install -m 600 /dev/null "$log"
	AIRLOK_JOB_ID=$id AIRLOK_PANEL_ENV=$panel timeout --kill-after=30 "$RESTART_TIMEOUT" "$RECIPES_DIR/app-restart.sh" "$app" \
		</dev/null >"$log" 2>&1
	rc=$?
	rm -f "$PANEL_ENV"
	if ((rc == 124 || rc == 137)); then
		message="перезапуск не уложился в $((RESTART_TIMEOUT / 60)) минут и был остановлен"
	fi
	if ((rc == 0)); then
		send_result "$id" success "$message" "$log"
	else
		send_result "$id" failed "$message" "$log"
	fi
}

# run_remove — убрать приложение с сервера: после успеха дашборд удаляет его и у себя.
run_remove() {
	local id=$1 app=$2 with_data=$3 log=$CMD_LOG rc message="" args=()
	[[ $with_data == 1 ]] && args=(--with-data)
	echo "команда ${id}: удаление ${app}${args:+ вместе с данными}"
	install -m 600 /dev/null "$log"
	timeout --kill-after=30 "$REMOVE_TIMEOUT" "$RECIPES_DIR/app-remove.sh" "$app" "${args[@]}" </dev/null >"$log" 2>&1
	rc=$?
	if ((rc == 124 || rc == 137)); then
		message="удаление не уложилось в $((REMOVE_TIMEOUT / 60)) минут и было остановлено"
	fi
	if ((rc == 0)); then
		send_result "$id" success "$message" "$log"
	else
		send_result "$id" failed "$message" "$log"
	fi
}

# run_db_query — запрос только на чтение к базе приложения для дашборда. Ответ — строки или
# ошибка — уходит отдельной ручкой: человек ждёт его на открытой странице.
run_db_query() {
	local id=$1 out=$RUN_DIR/dbquery.out err=$RUN_DIR/dbquery.err code
	echo "команда ${id}: запрос к базе"
	timeout --kill-after=5 "$DBQUERY_TIMEOUT" python3 "$RECIPES_DIR/dbquery.py" <"$DBQ_JSON" >"$out" 2>"$err"
	if [[ ! -s $out ]]; then
		python3 -c 'import json, sys; json.dump({"error": {"kind": "failed", "message": "запрос не выполнился: " + sys.stdin.read()[-1000:]}}, sys.stdout)' \
			<"$err" >"$out"
	fi
	# Ответ бывает в несколько мегабайт: ждём дольше обычных 10 секунд.
	code=$(dashboard_fetch POST "/agent/v1/db-queries/${id}/result" /dev/null "@${out}" 60) || code=000
	rm -f "$DBQ_JSON" "$out" "$err"
	[[ $code == 2?? ]] || echo "ответ на запрос к базе не принят (код ${code})"
}

# run_ssh_keys — ключи устройств из дашборда в authorized_keys root: дашборд присылает полный
# список, чужие ключи на сервере sshkeys.py не трогает.
run_ssh_keys() {
	# Свой журнал: ключи могут прийти, пока идёт закрытие пароля, а у него журнал команды занят.
	local id=$1 log=$STATE_DIR/ssh-keys.log
	echo "команда ${id}: ключи устройств"
	install -m 600 /dev/null "$log"
	if timeout --kill-after=5 "$SSHKEYS_TIMEOUT" python3 "$RECIPES_DIR/sshkeys.py" <"$KEYS_JSON" >"$log" 2>&1; then
		send_result "$id" success "" "$log"
	else
		send_result "$id" failed "" "$log"
	fi
	rm -f "$KEYS_JSON"
}

# wait_key_login НАЧАЛО ЖУРНАЛ — дождаться входа по ключу устройства. Коды как у wait-login.
# Команды агент выполняет по одной, но ключи ждать нельзя: человек открывает серверу устройство,
# пока идёт это ожидание, и без его ключа войти ему нечем. Остальные команды подождут.
wait_key_login() {
	local since=$1 log=$2 deadline=$((SECONDS + SSH_LOGIN_WAIT)) keys=$RUN_DIR/next-keys.json left rc code
	while :; do
		code=$(dashboard_fetch GET "/agent/v1/commands/next?wait=0&only=ssh_keys" "$keys" "" 20) || code=000
		[[ $code == 200 ]] && handle "$keys"
		rm -f "$keys"
		left=$((deadline - SECONDS))
		((left > 0)) || return 2
		python3 "$RECIPES_DIR/sshkeys.py" wait-login "$since" $((left < SSH_LOGIN_SLICE ? left : SSH_LOGIN_SLICE)) >>"$log" 2>&1
		rc=$?
		((rc == 2)) || return "$rc"
	done
}

# run_ssh_password — закрыть или открыть вход по паролю по кнопке в дашборде. Закрытие
# закрепляется, только если на сервер вошли новым соединением по ключу устройства; иначе
# пароль возвращается сразу, не дожидаясь таймера отката.
run_ssh_password() {
	local id=$1 login=$2 user=${3:-root} log=$CMD_LOG since rc
	echo "команда ${id}: вход по паролю — ${login}"
	install -m 600 /dev/null "$log"
	if [[ $login == open ]]; then
		if timeout 60 "$RECIPES_DIR/server-base.sh" ssh-open </dev/null >"$log" 2>&1; then
			send_result "$id" success "вход по паролю открыт" "$log"
		else
			send_result "$id" failed "" "$log"
		fi
		heartbeat_now
		return
	fi
	since=$(date +%s)
	if ! AIRLOK_SSH_CONFIRM=agent AIRLOK_SSH_USER=$user AIRLOK_SSH_ROLLBACK_SECONDS=$((SSH_LOGIN_WAIT + 60)) \
		timeout 120 "$RECIPES_DIR/server-base.sh" ssh </dev/null >"$log" 2>&1; then
		send_result "$id" failed "" "$log"
		return
	fi
	if ! systemctl is-active --quiet airlok-ssh-rollback.timer; then
		# Таймера нет: пароль был закрыт раньше или на сервере нет ключей — server-base.sh сказал почему.
		if grep -qx 'passwordauthentication no' <<<"$(sshd -T 2>/dev/null)"; then
			send_result "$id" success "вход по паролю уже закрыт" "$log"
		else
			send_result "$id" failed "вход по паролю не закрыт: на сервере нет SSH-ключей ${user}" "$log"
		fi
		heartbeat_now
		return
	fi
	heartbeat_now
	wait_key_login "$since" "$log"
	rc=$?
	if ((rc == 0)); then
		"$RECIPES_DIR/server-base.sh" confirm-ssh >>"$log" 2>&1
		send_result "$id" success "вход по паролю закрыт, вход по ключу проверен" "$log"
	else
		systemctl start airlok-ssh-rollback.service >>"$log" 2>&1
		systemctl stop airlok-ssh-rollback.timer 2>/dev/null
		if ((rc == 2)); then
			send_result "$id" failed "за $((SSH_LOGIN_WAIT / 60)) минут на сервер не вошли по ключу устройства — вход по паролю вернули" "$log"
		else
			send_result "$id" failed "" "$log"
		fi
	fi
	heartbeat_now
}

# run_data_move — данные приложения: выгрузить и загрузить при переезде, запустить остановленное
# снова, сделать копию базы или восстановить её. Ссылки на хранилище — только в окружении app-data.sh.
run_data_move() {
	local id=$1 op=$2 app=$3 db_url=$4 files_url=$5 data_url=$6 log=$CMD_LOG rc message=""
	echo "команда ${id}: данные ${app} — ${op}"
	install -m 600 /dev/null "$log"
	AIRLOK_DB_URL=$db_url AIRLOK_FILES_URL=$files_url AIRLOK_DATA_URL=$data_url timeout --kill-after=60 "$DATA_TIMEOUT" \
		"$RECIPES_DIR/app-data.sh" "$op" "$app" </dev/null >"$log" 2>&1 &
	recipe_wait $!
	rc=$?
	if ((rc == 124 || rc == 137)); then
		message="работа с данными не уложилась в $((DATA_TIMEOUT / 60)) минут и была остановлена"
	fi
	if ((rc == 0)); then
		send_result "$id" success "$message" "$log"
	else
		send_result "$id" failed "$message" "$log"
	fi
}

# heartbeat_now — отметка с состоянием сервера сразу, а не через минуту: дашборд покажет,
# открыт ли теперь вход по паролю.
heartbeat_now() {
	systemctl start --no-block airlok-healthcheck.service 2>/dev/null
}

# os_job_result ID СТАТУС [СООБЩЕНИЕ] — итог команды с системой. Сначала отметка: к моменту итога
# дашборд уже должен знать, остались ли обновления и нужна ли перезагрузка.
os_job_result() {
	timeout 120 systemctl start airlok-healthcheck.service 2>/dev/null
	send_result "$1" "$2" "${3:-}" "$OS_LOG"
	rm -f "$OS_JOB" "$OS_RC" "$OS_APPS" "$OS_STATUS"
}

# finish_os_upgrade ID — дождаться службы с установкой и отправить итог. Нет файла с кодом — служба
# не дошла до конца: её оборвал таймаут или выключение сервера.
finish_os_upgrade() {
	local id=$1 rc streamer
	stream_progress "$id" "$OS_LOG" "" "$OS_STATUS" &
	streamer=$!
	while systemctl is-active --quiet "$OS_UNIT"; do
		sleep 3
	done
	kill "$streamer" 2>/dev/null
	wait "$streamer" 2>/dev/null
	rc=$(cat "$OS_RC" 2>/dev/null)
	case $rc in
	0) os_job_result "$id" success ;;
	"") os_job_result "$id" failed "установка оборвалась, не дойдя до конца. Запустите обновление ещё раз: оно продолжит с того же места" ;;
	*) os_job_result "$id" failed ;;
	esac
}

# run_os_upgrade — поставить обновления отдельной службой: у неё свой cgroup, и перезапуск агента
# её не оборвёт, иначе dpkg остановился бы на полпути. Nice и idle — чтобы приложения не тормозили.
run_os_upgrade() {
	local id=$1
	echo "команда ${id}: обновления системы"
	install -m 600 /dev/null "$OS_LOG"
	rm -f "$OS_RC" "$OS_STATUS"
	printf '%s upgrade\n' "$id" >"$OS_JOB"
	systemctl reset-failed "$OS_UNIT" 2>/dev/null
	# shellcheck disable=SC2016 # аргументы раскрывает bash внутри службы
	if ! systemd-run --quiet --collect --unit="$OS_UNIT" --property=Nice=10 --property=IOSchedulingClass=idle \
		--property=RuntimeMaxSec="$OS_UPGRADE_TIMEOUT" --setenv=AIRLOK_OS_STATUS="$OS_STATUS" \
		/bin/bash -c '"$1" upgrade >"$2" 2>&1; echo $? >"$3"' _ "$RECIPES_DIR/os-upgrade.sh" "$OS_LOG" "$OS_RC" >>"$OS_LOG" 2>&1; then
		os_job_result "$id" failed "не удалось запустить установку"
		return
	fi
	finish_os_upgrade "$id"
}

# run_os_reboot — перезагрузка. Итог отправит агент уже после неё: успех значит, что сервер вернулся.
run_os_reboot() {
	local id=$1
	echo "команда ${id}: перезагрузка сервера"
	install -m 600 /dev/null "$OS_LOG"
	printf '%s reboot %s\n' "$id" "$(cat /proc/sys/kernel/random/boot_id)" >"$OS_JOB"
	if ! "$RECIPES_DIR/os-upgrade.sh" reboot "$OS_APPS" </dev/null >"$OS_LOG" 2>&1; then
		os_job_result "$id" failed
		return
	fi
	sleep "$OS_REBOOT_WAIT"
	os_job_result "$id" failed "сервер не перезагрузился: перезагрузите его в панели хостера"
}

# resume_os_job — агент запустился, а команда с системой не закрыта: его перезапустило обновление
# или команда была перезагрузкой.
resume_os_job() {
	local id action boot
	[[ -s $OS_JOB ]] || return 0
	read -r id action boot <"$OS_JOB"
	echo "команда ${id}: продолжаем после запуска агента"
	if [[ $action == upgrade ]]; then
		finish_os_upgrade "$id"
		return
	fi
	if [[ $boot == "$(cat /proc/sys/kernel/random/boot_id)" ]]; then
		sleep "$OS_REBOOT_WAIT"
		os_job_result "$id" failed "сервер не перезагрузился: перезагрузите его в панели хостера"
		return
	fi
	if "$RECIPES_DIR/os-upgrade.sh" check-apps "$OS_APPS" </dev/null >"$OS_LOG" 2>&1; then
		os_job_result "$id" success
	else
		os_job_result "$id" failed
	fi
}

# run_update — обновление агента, которое человек подтвердил в дашборде. После отчёта агент
# перезапускается: так он продолжит на новом коде, даже если сам agent.sh не менялся.
run_update() {
	local id=$1 sha=${2:-} log=$CMD_LOG rc message=""
	echo "команда ${id}: обновление агента${sha:+ до ${sha}}"
	install -m 600 /dev/null "$log"
	timeout --kill-after=60 "$UPDATE_TIMEOUT" "$RECIPES_DIR/update.sh" ${sha:+--to "$sha"} </dev/null >"$log" 2>&1
	rc=$?
	if ((rc == 124 || rc == 137)); then
		message="обновление не уложилось в $((UPDATE_TIMEOUT / 60)) минут и было остановлено"
	fi
	if ((rc != 0)); then
		send_result "$id" failed "$message" "$log"
		return
	fi
	send_result "$id" success "$message" "$log"
	# flusher — local из handle(): дальше этот процесс заменит новый код, и очередь он досылает сам.
	[[ -n ${flusher:-} ]] && kill "$flusher" 2>/dev/null
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

# audit РЕШЕНИЕ ID ВИД [ПОДРОБНОСТИ] — строка в журнал команд, без токенов и ссылок на хранилище.
audit() {
	local size
	size=$(stat -c %s "$AUDIT_LOG" 2>/dev/null) || install -m 640 /dev/null "$AUDIT_LOG"
	((${size:-0} < AUDIT_MAX_BYTES)) || { mv -f "$AUDIT_LOG" "$AUDIT_LOG.1"; install -m 640 /dev/null "$AUDIT_LOG"; }
	printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >>"$AUDIT_LOG"
}

# refuse ID ВИД ПРИЧИНА — ответить дашборду, что команду сервер не выполнит. Запрос к базе ждёт
# ответа на своей ручке: человек смотрит на открытую страницу.
refuse() {
	local id=$1 kind=$2 why=$3 out=$RUN_DIR/dbquery.out
	echo "команда ${id} (${kind}) не выполнена: ${why}"
	if [[ $kind == db_query ]]; then
		python3 -c 'import json, sys; json.dump({"error": {"kind": "failed", "message": sys.argv[1]}}, sys.stdout)' "$why" >"$out"
		dashboard_fetch POST "/agent/v1/db-queries/${id}/result" /dev/null "@${out}" >/dev/null
		rm -f "$out"
		return
	fi
	send_result "$id" failed "$why"
}

# long_running — идёт ли фоновая долгая команда. По таблице заданий bash, а не kill -0: номер
# завершившегося процесса мог уже достаться другому. jobs — в файл, а не через $(…): в подоболочке
# таблица заданий не та же.
long_running() {
	[[ -n $LONG_PID ]] || return 1
	jobs -rp >"$RUN_DIR/jobs" 2>/dev/null
	grep -qx "$LONG_PID" "$RUN_DIR/jobs" 2>/dev/null
}

# reap_long — забыть завершившуюся долгую команду: её итог она отправила сама.
reap_long() {
	[[ -n $LONG_PID ]] || return 0
	long_running && return 0
	wait "$LONG_PID" 2>/dev/null
	LONG_PID="" LONG_APP="" LONG_ID="" LONG_KIND=""
}

# wait_long [ID] — дождаться долгой команды: прежний порядок «по одной». Ждать можно и час, а итоги
# быстрых команд дашборд закрывает через 10–15 минут: пока ждём, их досылает фон.
wait_long() {
	[[ -n $LONG_PID ]] || return 0
	local flusher=""
	[[ -n ${1:-} ]] && echo "команда ${1}: ждёт конца команды ${LONG_ID}"
	if results_pending; then
		results_flusher &
		flusher=$!
	fi
	wait "$LONG_PID" 2>/dev/null
	if [[ -n $flusher ]]; then
		kill "$flusher" 2>/dev/null
		wait "$flusher" 2>/dev/null
	fi
	LONG_PID="" LONG_APP="" LONG_ID="" LONG_KIND=""
}

# must_wait_long ВИД ПРИЛОЖЕНИЕ — команду нельзя выполнять рядом с идущей долгой: вторая долгая,
# обновление агента или системы, перезапуск и удаление собираемого приложения. Дашборд с опросом по
# списку видов их посреди долгой не выдаёт, но другой мог — тогда выполняем по одной, как раньше.
must_wait_long() {
	[[ -n $LONG_PID ]] || return 1
	case $1 in
	db_query | ssh_keys | ssh_password) return 1 ;;
	restart | remove) [[ $2 == "$LONG_APP" ]] ;;
	*) return 0 ;;
	esac
}

# long_job ФУНКЦИЯ АРГУМЕНТЫ… — тело фоновой долгой команды. TERM эта оболочка переживает и сама
# пересылает рецепту (recipe_wait), а потом отправляет итог «прервано». Больше никто её потомков
# вслепую не трогает: под удар попали бы шаги, которыми она сохраняет итог.
long_job() {
	local ENV_JSON=$LONG_ENV_JSON PANEL_ENV=$LONG_PANEL_ENV ENV_ERR=$LONG_ENV_ERR
	trap 'LONG_STOP=1; [[ -n ${LONG_CHILD:-} ]] && stop_recipe TERM "$LONG_CHILD"' TERM
	"$@"
}

# stop_recipe СИГНАЛ PID — сигнал рецепту долгой команды: всей группе timeout, а не только ему.
# Пересылать сигнал своему рецепту timeout не обязан — uutils из Ubuntu 26.04 этого и не делает.
stop_recipe() {
	kill -"$1" -- "-$2" 2>/dev/null || kill -"$1" "$2" 2>/dev/null
}

# recipe_wait PID — дождаться рецепта долгой команды, запущенного в фоне. Ожидание обычной команды
# сигнал не прерывает, а wait прерывает: так trap в long_job пересылает TERM рецепту сразу, а не
# после его конца. Остановленному рецепту — STOP_GRACE секунд, потом KILL всей его группе (timeout
# заводит свою), чтобы итог успел уйти до того, как systemd добьёт службу.
recipe_wait() {
	local rc killer=""
	LONG_CHILD=$1
	while :; do
		wait "$LONG_CHILD"
		rc=$?
		if ((rc > 128)) && kill -0 "$LONG_CHILD" 2>/dev/null; then
			if [[ ${LONG_STOP:-} == 1 && -z $killer ]]; then
				# sleep — в фоне со своим trap: kill этой подоболочки иначе оставил бы его висеть в cgroup службы.
				(
					trap 'kill "$sleeper" 2>/dev/null; exit 0' TERM
					sleep "$STOP_GRACE" &
					sleeper=$!
					wait "$sleeper"
					stop_recipe KILL "$1"
				) 2>/dev/null &
				killer=$!
			fi
			continue
		fi
		break
	done
	[[ -n $killer ]] && kill "$killer" 2>/dev/null
	LONG_CHILD=""
	return "$rc"
}

# start_long ID ПРИЛОЖЕНИЕ ФУНКЦИЯ АРГУМЕНТЫ… — долгая команда в фоне.
start_long() {
	local id=$1 app=$2
	shift 2
	[[ -e $ENV_JSON ]] && mv -f "$ENV_JSON" "$LONG_ENV_JSON"
	long_job "$@" &
	LONG_PID=$! LONG_APP=$app LONG_ID=$id LONG_KIND=${CMD_KIND:-unknown}
}

# stop_agent — службу останавливают: долгой команде дать оборвать рецепт и записать итог. Не успела —
# итог «прервано» пишет основной цикл: без итога выкатку без хода дашборд выдал бы снова, а с ходом —
# час показывал бы «выполняется». Если итог уже ушёл, дашборд второй просто не примет.
stop_agent() {
	local deadline=$((SECONDS + STOP_GRACE + 15))
	if long_running; then
		kill -TERM "$LONG_PID" 2>/dev/null
		while long_running && ((SECONDS < deadline)); do
			sleep 0.2
		done
	fi
	if [[ -n $LONG_ID ]] && ! sent_already "$LONG_ID"; then
		if [[ $LONG_KIND == deploy ]] && job_pending "$LONG_ID" "airlok-publish-${LONG_APP}"; then
			resume_later "$LONG_ID" deploy "airlok-publish-${LONG_APP}" "$STATE_DIR/command.log"
		else
			LONG_STOP=1 CMD_KIND=$LONG_KIND send_result "$LONG_ID" failed ""
		fi
	fi
	stop_current
	exit 0
}

# stop_current — итог быстрой команды, которую основной цикл выполнял в миг остановки: TERM оборвал её
# рецепт, а до своего send_result она уже не дойдёт. Обновление системы и перезагрузку доводит
# resume_os_job, перезапуск приложения, который идёт своей службой, — resume_jobs.
stop_current() {
	[[ -n $CUR_ID ]] && ! sent_already "$CUR_ID" || return 0
	case $CUR_KIND in
	os_upgrade) ;;
	db_query) refuse "$CUR_ID" db_query "$STOP_MESSAGE" ;;
	restart)
		if job_pending "$CUR_ID" "airlok-restart-${CUR_APP}"; then
			resume_later "$CUR_ID" restart "airlok-restart-${CUR_APP}" "$CUR_LOG"
		else
			LONG_STOP=1 CMD_KIND=$CUR_KIND send_result "$CUR_ID" failed "" "$CUR_LOG"
		fi
		;;
	*) LONG_STOP=1 CMD_KIND=$CUR_KIND send_result "$CUR_ID" failed "" "$CUR_LOG" ;;
	esac
}

handle() {
	# CMD_KIND видит send_result: local, чтобы ключи, принятые посреди другой команды, не подменили её вид.
	local f=() why details app="" flusher="" CMD_KIND=unknown
	rm -f "$ENV_JSON" "$DBQ_JSON" "$KEYS_JSON"
	mapfile -d '' -t f < <(python3 -c "$PARSE_PY" "$ENV_JSON" "$DBQ_JSON" "$KEYS_JSON" <"$1")
	rm -f "$1"
	[[ ${f[0]:-} == ok ]] && CMD_KIND=${f[2]:-unknown}
	[[ -n ${f[1]:-} ]] && drop_result "${f[1]}"
	if [[ ${f[0]:-} != ok ]]; then
		echo "команда ${f[1]:-без id} отклонена: ${f[2]:-ответ дашборда не разобрать}"
		[[ -n ${f[1]:-} ]] && send_result "${f[1]}" failed "${f[2]:-}"
		return
	fi
	case ${f[2]:-} in
	update) details="sha=${f[3]:-}" ;;
	deploy) details="app=${f[3]} repo=${f[4]} sha=${f[5]} domain=${f[10]}" ;;
	restart) details="app=${f[3]}" ;;
	remove) details="app=${f[3]} with_data=${f[4]}" ;;
	data_move) details="op=${f[3]} app=${f[4]}" ;;
	ssh_password) details="login=${f[3]} user=${f[4]}" ;;
	os_upgrade) details="action=${f[3]}" ;;
	*) details="" ;;
	esac
	# Сбой проверки — тоже отказ: в строгом режиме лучше не выполнить команду, чем выполнить лишнюю.
	if ! why=$(python3 "$RECIPES_DIR/strict.py" check "${f[2]}" "${f[3]:-}" "${f[4]:-}" 2>&1); then
		audit refused "${f[1]}" "${f[2]}" "$details"
		refuse "${f[1]}" "${f[2]}" "${why:-не удалось проверить настройки строгого режима}"
		rm -f "$ENV_JSON" "$DBQ_JSON" "$KEYS_JSON"
		return
	fi
	audit accepted "${f[1]}" "${f[2]}" "$details"
	# Переменные из дашборда могут увести данные приложения на чужой сервер, а ключ для них публичный:
	# в строгом режиме значения задаются только на сервере, в shared/user.env.
	if strict_mode; then
		rm -f "$ENV_JSON"
		case ${f[2]} in
		deploy) f[11]=0 ;;
		restart) f[4]=0 ;;
		esac
	fi
	case ${f[2]} in
	data_move) app=${f[4]} ;;
	deploy | restart | remove) app=${f[3]} ;;
	esac
	must_wait_long "${f[2]}" "$app" && wait_long "${f[1]}"
	[[ -n $LONG_PID ]] && local CMD_LOG=$SIDE_LOG
	if results_pending; then
		results_flusher &
		flusher=$!
	fi
	# Ключи, принятые посреди закрытия пароля, — вложенный handle: текущей остаётся внешняя команда.
	local own=0
	if [[ -z $CUR_ID && ${f[2]} != deploy && ${f[2]} != data_move ]]; then
		CUR_ID=${f[1]} CUR_KIND=${f[2]} CUR_APP=$app CUR_LOG=$CMD_LOG own=1
		sent_trim
	fi
	case ${f[2]:-} in
	update) run_update "${f[1]}" "${f[3]:-}" ;;
	restart) run_restart "${f[1]}" "${f[3]}" "${f[4]}" ;;
	remove) run_remove "${f[1]}" "${f[3]}" "${f[4]}" ;;
	db_query) run_db_query "${f[1]}" ;;
	ssh_keys) run_ssh_keys "${f[1]}" ;;
	ssh_password) run_ssh_password "${f[1]}" "${f[3]}" "${f[4]}" ;;
	data_move) start_long "${f[1]}" "$app" run_data_move "${f[1]}" "${f[3]}" "${f[4]}" "${f[5]}" "${f[6]}" "${f[7]}" ;;
	os_upgrade)
		if [[ ${f[3]} == reboot ]]; then
			run_os_reboot "${f[1]}"
		else
			run_os_upgrade "${f[1]}"
		fi
		;;
	*) start_long "${f[1]}" "$app" run_deploy "${f[1]}" "${f[@]:3}" ;;
	esac
	if [[ -n $flusher ]]; then
		kill "$flusher" 2>/dev/null
		wait "$flusher" 2>/dev/null
	fi
	((own)) && CUR_ID="" CUR_KIND="" CUR_APP="" CUR_LOG=""
	return 0
}

# sent_trim — список отправленных итогов нужен только для идущих команд: не даём ему расти.
sent_trim() {
	local list=$RUN_DIR/sent
	[[ -f $list ]] && (($(wc -l <"$list") > 200)) || return 0
	tail -n 50 "$list" >"$list.tmp" && mv -f "$list.tmp" "$list"
}

# side_poll — опрос, пока идёт долгая команда или не доставлен итог выкатки: только быстрые команды и без
# возврата «потерянных» выкаток в очередь, который дашборд делает при обычном опросе.
side_poll() {
	local resp=$RUN_DIR/next.json query code started pause
	if [[ $SIDE_POLL != list ]] && ((SECONDS - SIDE_POLL_AT > SIDE_RECHECK)); then
		SIDE_POLL=list
	fi
	case $SIDE_POLL in
	list) query="only=${QUICK_KINDS}${LONG_APP:+&busy_app=${LONG_APP}}" ;;
	keys) query=only=ssh_keys ;;
	*)
		if [[ -n $LONG_PID ]]; then
			wait_long
		else
			pause=$(jitter "$BACKOFF")
			echo "итог выкатки ещё не доставлен, новые команды подождут: повтор через ${pause} с"
			sleep "$pause"
			BACKOFF=$((BACKOFF * 2 > 60 ? 60 : BACKOFF * 2))
		fi
		return
		;;
	esac
	started=$SECONDS
	code=$(dashboard_fetch GET "/agent/v1/commands/next?wait=${SIDE_WAIT}&${query}" "$resp" "" $((SIDE_WAIT + 20))) || code=000
	case $code in
	200)
		BACKOFF=5
		handle "$resp"
		;;
	204)
		BACKOFF=5
		((SECONDS - started < 2)) && sleep 5
		;;
	400)
		# Дашборд старее агента и этого опроса не знает. Список видов он отвергает целиком, поэтому
		# команды не выдал: дальше — только ключи устройств, а без них — не опрашиваем.
		if [[ $SIDE_POLL == list ]]; then
			echo "дашборд не знает опроса ${query}: посреди долгой команды берём только ключи устройств"
			SIDE_POLL=keys
		else
			echo "дашборд не знает опроса ${query}: команды выполняются по одной"
			SIDE_POLL=none
		fi
		SIDE_POLL_AT=$SECONDS
		;;
	*)
		pause=$(jitter "$BACKOFF")
		echo "дашборд недоступен (код ${code}), повтор через ${pause} с"
		sleep "$pause"
		BACKOFF=$((BACKOFF * 2 > 60 ? 60 : BACKOFF * 2))
		;;
	esac
	rm -f "$resp"
}

# poll_once — один шаг основного цикла.
poll_once() {
	local resp=$RUN_DIR/next.json code started pause
	reap_long
	# Обновление заменяет файл новым: между командами перезапускаемся на свежем коде. Посреди долгой
	# команды — нет: новый процесс о ней не знал бы и взял бы вторую.
	if [[ -z $LONG_PID && $(stat -c %i "$SELF" 2>/dev/null) != "$SELF_INODE" ]]; then
		echo "рецепты обновились, перезапуск"
		exec "$SELF"
	fi
	if [[ ! -r $DASHBOARD_CONF ]]; then
		sleep 30
		return
	fi
	flush_results
	if [[ -n $LONG_PID ]] || results_hold; then
		side_poll
		return
	fi
	started=$SECONDS
	code=$(dashboard_fetch GET "/agent/v1/commands/next?wait=${WAIT}" "$resp" "" $((WAIT + 20))) || code=000
	case $code in
	200)
		BACKOFF=5
		handle "$resp"
		;;
	204)
		BACKOFF=5
		# Дашборд, который не держит запрос, отвечает сразу — не засыпаем его запросами.
		((SECONDS - started < 2)) && sleep 5
		;;
	401 | 403)
		echo "дашборд не принимает токен сервера, следующая попытка через 5 минут"
		sleep "$(jitter 300)"
		;;
	404)
		# Дашборд без команд для агента: проверим, не обновился ли он.
		sleep "$(jitter 300)"
		;;
	*)
		pause=$(jitter "$BACKOFF")
		echo "дашборд недоступен (код ${code}), повтор через ${pause} с"
		sleep "$pause"
		BACKOFF=$((BACKOFF * 2 > 300 ? 300 : BACKOFF * 2))
		;;
	esac
	rm -f "$resp"
}

main() {
	need_root
	install -d -m 700 "$RUN_DIR" "$STATE_DIR"
	install -d -m 700 "$RESULTS_DIR"
	rm -f "$RESULTS_DIR"/.*.tmp
	trap stop_agent TERM INT
	echo "агент запущен, версия $(agent_version)"
	if ensure_env_key; then
		echo "ключ переменных окружения: $(env_key_id) — тот же отпечаток показывает дашборд"
	else
		echo "не удалось создать ключ ${ENV_KEY}: переменные из дашборда не расшифровать"
	fi
	[[ -r $DASHBOARD_CONF ]] && resume_os_job
	[[ -r $DASHBOARD_CONF ]] && resume_jobs
	while :; do
		poll_once
	done
}

main "$@"
