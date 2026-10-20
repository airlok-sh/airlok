#!/usr/bin/env bash
# Проверка сервера только на чтение: ничего не меняет, никуда не отправляет данные
# и не ходит в сеть. Запуск: sudo bash checkup.sh | tee checkup-report.txt

set -uo pipefail
export LC_ALL=C

VERSION="0.1.0"

CHECKED=0
FOUND=0
CRIT=()
HIGH=()
LOW=()
ASK=()
SKIPS=()
PUBLIC_PORTS=()
FW_CLOSED_PORTS=()
declare -A REPORTED_PORTS=()
UFW_ACTIVE=no
UFW_ALLOWED=""
NGINX_CONF=""
PROJECT_DIRS=(/root /home /opt /srv /var/www /app)

DB_PORTS="5432 3306 6379 27017 9200 9300 11211 5984 8086 5672 15672 6380 26379 7474 7687 8123 9000 2379"
DEV_PORTS="3000 3001 4173 5000 5173 8000 8080 8888"

if [[ -t 1 ]]; then
	C_RED=$'\e[31m' C_YEL=$'\e[33m' C_BLU=$'\e[36m' C_DIM=$'\e[2m' C_B=$'\e[1m' C_0=$'\e[0m'
else
	C_RED="" C_YEL="" C_BLU="" C_DIM="" C_B="" C_0=""
fi

has() { command -v "$1" >/dev/null 2>&1; }
is_root() { [[ $EUID -eq 0 ]]; }
checked() { CHECKED=$((CHECKED + 1)); }
skip() { SKIPS+=("$1: $2"); }
in_list() { [[ " $2 " == *" $1 "* ]]; }

# timeout есть не везде (busybox, старые образы), без него просто запускаем команду.
run() {
	if has timeout; then timeout 15 "$@"; else "$@"; fi
}

# finding LEVEL TITLE RISK FIX [EVIDENCE]
finding() {
	local level=$1 title=$2 risk=$3 fix=$4 evidence=${5:-}
	local block="${C_B}${title}${C_0}"
	[[ -n $evidence ]] && block+=$'\n'"   Найдено: ${evidence}"
	block+=$'\n'"   Чем грозит: ${risk}"$'\n'"   Что сделать: ${fix}"
	FOUND=$((FOUND + 1))
	case $level in
	crit) CRIT+=("$block") ;;
	high) HIGH+=("$block") ;;
	*) LOW+=("$block") ;;
	esac
}

ask() { ASK+=("$1"); }

# Почта в комментарии ключа — персональные данные, в отчёт идёт только первая буква.
mask_emails() {
	sed -E 's/([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+)/\1***@\2/g'
}

port_name() {
	case $1 in
	5432) echo "PostgreSQL" ;;
	3306) echo "MySQL/MariaDB" ;;
	6379 | 6380) echo "Redis" ;;
	26379) echo "Redis Sentinel" ;;
	27017) echo "MongoDB" ;;
	9200 | 9300) echo "Elasticsearch" ;;
	11211) echo "Memcached" ;;
	5984) echo "CouchDB" ;;
	8086) echo "InfluxDB" ;;
	5672) echo "RabbitMQ" ;;
	15672) echo "панель RabbitMQ" ;;
	7474 | 7687) echo "Neo4j" ;;
	8123) echo "ClickHouse" ;;
	9000) echo "ClickHouse/MinIO/Portainer" ;;
	2379) echo "etcd" ;;
	2375) echo "Docker API" ;;
	2376) echo "Docker API (TLS)" ;;
	*) echo "" ;;
	esac
}

# Адрес из ss/docker → public | local. Приватные сети считаем внутренними:
# снаружи они не видны, а сеть хостера — отдельный разговор.
addr_scope() {
	local a=${1#[}
	a=${a%]}
	a=${a%%%*}
	a=${a#::ffff:}
	case $a in
	"" | "*" | 0.0.0.0 | "::") echo public ;;
	127.* | ::1 | localhost) echo local ;;
	10.* | 192.168.* | 172.1[6-9].* | 172.2[0-9].* | 172.3[01].* | fd* | fe80*) echo local ;;
	*) echo public ;;
	esac
}

load_ufw() {
	has ufw && is_root || return
	local status
	status=$(ufw status verbose 2>/dev/null)
	grep -q "Status: active" <<<"$status" || return
	grep -Eq "Default: (deny|reject) \(incoming\)" <<<"$status" || return
	UFW_ACTIVE=yes
	UFW_ALLOWED=$(awk '/ALLOW IN/ && $1 ~ /^[0-9]/ {print $1}' <<<"$status")
}

# Порты, опубликованные Docker, ufw не фильтрует, поэтому функция только для
# обычных процессов.
ufw_allows() {
	local port=$1 rule p
	local -a parts
	while read -r rule; do
		[[ -z $rule ]] && continue
		IFS=',' read -ra parts <<<"${rule%%/*}"
		for p in "${parts[@]}"; do
			if [[ $p == *:* ]]; then
				((port >= ${p%:*} && port <= ${p#*:})) && return 0
			elif [[ $p == "$port" ]]; then
				return 0
			fi
		done
	done <<<"$UFW_ALLOWED"
	return 1
}

report_public_port() {
	local port=$1 who=$2 via=$3
	local name
	name=$(port_name "$port")
	if [[ $port == 2375 ]]; then
		finding crit "Docker API открыт в интернет (порт 2375)" \
			"Любой, кто найдёт порт, получает полный контроль над сервером: запускает свои контейнеры с доступом к диску и всем данным." \
			"Убрать tcp://…:2375 из настроек dockerd (/etc/docker/daemon.json или systemd-юнит). Управлять Docker только локально." \
			"$who, $via"
	elif in_list "$port" "$DB_PORTS"; then
		local fix="Привязать к 127.0.0.1 или убрать публикацию порта, если база нужна только приложению."
		[[ $via == docker* ]] && fix="В docker-compose заменить \"${port}:${port}\" на \"127.0.0.1:${port}:${port}\" или убрать ports совсем, если база нужна только другим контейнерам. Файрвол ufw такой порт не закрывает: Docker обходит его правила."
		finding crit "${name:-Сервис} доступен из интернета (порт ${port})" \
			"Базу или кэш ищут боты круглосуточно. Пароль по умолчанию или его отсутствие означает утечку или удаление данных, часто с требованием выкупа." \
			"$fix" "$who, $via"
	elif in_list "$port" "$DEV_PORTS"; then
		finding low "Приложение доступно напрямую, в обход nginx и HTTPS (порт ${port})" \
			"Трафик идёт без шифрования, а заголовки безопасности и ограничения из nginx не действуют." \
			"Привязать приложение к 127.0.0.1 и открыть его только через nginx на 80/443." \
			"$who, $via"
	else
		return 1
	fi
	return 0
}

check_ssh() {
	local sshd
	sshd=$(command -v sshd || echo /usr/sbin/sshd)
	if [[ ! -x $sshd ]]; then
		skip "SSH" "sshd не найден"
		return
	fi
	if ! is_root; then
		skip "SSH" "для sshd -T нужны права root"
		return
	fi
	local conf
	if ! conf=$("$sshd" -T 2>/dev/null); then
		skip "SSH" "sshd -T завершился с ошибкой"
		return
	fi
	checked

	local pass kbd pam root
	pass=$(awk '$1=="passwordauthentication"{print $2}' <<<"$conf")
	kbd=$(awk '$1=="kbdinteractiveauthentication"||$1=="challengeresponseauthentication"{print $2; exit}' <<<"$conf")
	pam=$(awk '$1=="usepam"{print $2}' <<<"$conf")
	root=$(awk '$1=="permitrootlogin"{print $2}' <<<"$conf")

	local password_on=no
	if [[ $pass == yes ]] || [[ $kbd == yes && $pam == yes ]]; then
		password_on=yes
	fi

	local safe_fix="Сначала добавить свой ключ и проверить вход по нему в новом окне терминала, не закрывая текущее. Только потом поставить PasswordAuthentication no и KbdInteractiveAuthentication no и перезапустить sshd. Иначе можно потерять доступ к серверу."

	if [[ $password_on == yes && $root == yes ]]; then
		finding crit "Вход root по SSH с паролем" \
			"Боты круглосуточно подбирают пароль именно к root. Подобрали — получили сервер целиком." \
			"$safe_fix Затем завести обычного пользователя с sudo и поставить PermitRootLogin no." \
			"PasswordAuthentication=${pass}, PermitRootLogin=${root}"
	elif [[ $password_on == yes ]]; then
		finding crit "Вход по SSH с паролем включён" \
			"Пароли подбирают боты круглосуточно, слабый или повторно использованный пароль рано или поздно угадают." \
			"$safe_fix" "PasswordAuthentication=${pass}"
	elif [[ $root == yes ]]; then
		finding high "Вход root по SSH разрешён" \
			"Root — цель номер один для перебора, и при утечке ключа атакующий сразу получает полный доступ." \
			"Войти обычным пользователем с sudo, проверить, что sudo работает, и поставить PermitRootLogin no." \
			"PermitRootLogin=${root}"
	fi
	if [[ $root == prohibit-password || $root == without-password ]]; then
		finding low "Вход root по SSH разрешён по ключу" \
			"Пароль не подберут, но любая ошибка на сервере выполняется сразу с полными правами." \
			"Работать под обычным пользователем с sudo и поставить PermitRootLogin no." \
			"PermitRootLogin=${root}"
	fi
}

check_docker() {
	if ! has docker; then
		skip "Docker" "docker не установлен"
		return
	fi
	if ! run docker info >/dev/null 2>&1; then
		skip "Docker" "нет доступа к демону Docker (запустите через sudo)"
		return
	fi
	checked

	local name ports mapping host hport
	local -a mappings
	while IFS='|' read -r name ports; do
		[[ -z $ports ]] && continue
		IFS=',' read -ra mappings <<<"$ports"
		for mapping in "${mappings[@]}"; do
			mapping=${mapping# }
			[[ $mapping != *"->"* ]] && continue
			host=${mapping%%->*}
			hport=${host##*:}
			host=${host%:*}
			hport=${hport%%-*}
			[[ $(addr_scope "$host") == public ]] || continue
			[[ -n ${REPORTED_PORTS[$hport]:-} ]] && continue
			REPORTED_PORTS[$hport]=1
			local via="docker, контейнер ${name}"
			[[ $UFW_ACTIVE == yes ]] && via+=", ufw включён, но Docker его обходит"
			if ! report_public_port "$hport" "${host}:${hport}" "$via"; then
				PUBLIC_PORTS+=("${hport}  ${name} (docker)")
			fi
		done
	done < <(run docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null)

	local privileged=() sock=() norestart=() nologrot=() asroot=()
	local id fields
	for id in $(run docker ps -q 2>/dev/null); do
		fields=$(run docker inspect --format '{{.Name}}|{{.Config.User}}|{{.HostConfig.Privileged}}|{{.HostConfig.RestartPolicy.Name}}|{{.HostConfig.LogConfig.Type}}|{{index .HostConfig.LogConfig.Config "max-size"}}|{{range .Mounts}}{{.Source}};{{end}}' "$id" 2>/dev/null) || continue
		local cname user priv restart logtype maxsize mounts
		IFS='|' read -r cname user priv restart logtype maxsize mounts <<<"$fields"
		cname=${cname#/}
		[[ $priv == true ]] && privileged+=("$cname")
		[[ $mounts == *docker.sock* ]] && sock+=("$cname")
		[[ -z $restart || $restart == no ]] && norestart+=("$cname")
		[[ $logtype == json-file && -z $maxsize ]] && nologrot+=("$cname")
		[[ -z $user || $user == root || $user == 0 || $user == 0:* ]] && asroot+=("$cname")
	done

	if ((${#privileged[@]})); then
		finding high "Контейнеры запущены в режиме privileged" \
			"Взлом приложения внутри такого контейнера означает взлом всего сервера." \
			"Убрать privileged: true. Если контейнеру нужна конкретная возможность, выдать только её через cap_add." \
			"${privileged[*]}"
	fi
	if ((${#sock[@]})); then
		finding high "В контейнеры примонтирован docker.sock" \
			"Через сокет контейнер управляет Docker, а значит, всем сервером. Для traefik, portainer или watchtower это бывает нужно, для приложения — нет." \
			"Убрать /var/run/docker.sock из volumes у тех, кому он не нужен. Где нужен, монтировать только на чтение (:ro) или через docker-socket-proxy." \
			"${sock[*]}"
	fi
	if ((${#norestart[@]})); then
		finding high "Контейнеры не перезапускаются сами" \
			"После перезагрузки сервера или падения приложение не поднимется, и сайт будет лежать, пока кто-то не заметит." \
			"Добавить restart: unless-stopped в docker-compose для каждого сервиса." \
			"${norestart[*]}"
	fi
	if ((${#nologrot[@]})); then
		finding high "Логи контейнеров растут без ограничения" \
			"Логи Docker по умолчанию не ротируются и со временем заполняют диск. Тогда падает всё: база, приложение, SSH-вход." \
			"В /etc/docker/daemon.json задать \"log-opts\": {\"max-size\": \"10m\", \"max-file\": \"3\"} и пересоздать контейнеры (docker compose up -d --force-recreate)." \
			"${nologrot[*]}"
	fi
	if ((${#asroot[@]})); then
		finding low "Контейнеры работают от root" \
			"При взломе приложения атакующему проще выбраться из контейнера на сервер. Официальные образы баз (postgres, mysql, redis) и nginx сами понижают права, для них это нормально." \
			"Для своего приложения добавить в Dockerfile USER node (или другого непривилегированного пользователя)." \
			"${asroot[*]}"
	fi
}

check_ports() {
	if ! has ss; then
		skip "Открытые порты" "нет утилиты ss"
		return
	fi
	checked
	local local_addr process addr port who
	while read -r _ _ _ local_addr _ process; do
		port=${local_addr##*:}
		addr=${local_addr%:*}
		[[ $(addr_scope "$addr") == public ]] || continue
		[[ -n ${REPORTED_PORTS[$port]:-} ]] && continue
		REPORTED_PORTS[$port]=1
		who=$(grep -o '"[^"]*"' <<<"$process" | head -1 | tr -d '"')
		who=${who:-процесс не определён}
		if [[ $UFW_ACTIVE == yes ]] && ! ufw_allows "$port"; then
			FW_CLOSED_PORTS+=("${port}  ${who}")
			continue
		fi
		if ! report_public_port "$port" "$local_addr" "$who"; then
			PUBLIC_PORTS+=("${port}  ${who}")
		fi
	done < <(ss -H -tlnp 2>/dev/null)
	is_root || skip "Открытые порты" "без root не видно, какой процесс слушает порт"
}

check_firewall() {
	if ! is_root; then
		skip "Файрвол" "нужны права root"
		return
	fi
	checked
	[[ $UFW_ACTIVE == yes ]] && return
	if has ufw && [[ $(ufw status 2>/dev/null) == *"Status: active"* ]]; then
		finding high "Файрвол ufw включён, но входящие разрешены по умолчанию" \
			"Любой запущенный сервис сразу виден из интернета." \
			"ufw default deny incoming, затем ufw allow 22,80,443/tcp. Перед этим убедиться, что порт SSH в списке разрешённых." \
			"ufw status verbose"
		return
	fi
	if has nft && nft list ruleset 2>/dev/null | grep -Eq 'hook input .*policy drop'; then
		return
	fi
	if has iptables && iptables -S INPUT 2>/dev/null | grep -Eq '^-P INPUT (DROP|REJECT)'; then
		return
	fi
	local install=""
	has ufw || install="apt install ufw, затем "
	finding high "Файрвол не настроен, входящие соединения разрешены" \
		"Любой сервис, который вы или ваш ИИ запустите, сразу доступен из интернета: базы, панели, отладочные серверы." \
		"Включить файрвол: ${install}ufw allow 22/tcp, ufw allow 80,443/tcp, ufw default deny incoming, ufw enable. Порт SSH разрешить обязательно до ufw enable, иначе вход на сервер пропадёт. Если файрвол настроен в панели хостера, этот пункт можно принять как риск." \
		"ufw не включён, в nftables/iptables входящие не запрещены"
}

check_bruteforce() {
	if ! has systemctl; then
		skip "Защита от перебора" "нет systemctl"
		return
	fi
	checked
	local svc
	for svc in fail2ban crowdsec sshguard; do
		systemctl is-active --quiet "$svc" 2>/dev/null && return
	done
	finding low "Нет защиты от перебора паролей (fail2ban, CrowdSec)" \
		"Если вход по паролю уже выключен, риск невелик, но боты забивают логи и нагружают сервер." \
		"Установить fail2ban: apt install fail2ban, защита SSH включается по умолчанию."
}

check_updates() {
	if ! has apt-get; then
		skip "Обновления" "проверяется только на Debian и Ubuntu"
		return
	fi
	checked
	local installed=no enabled=no
	# Без grep -q в конвейере: при pipefail ранний выход grep валит apt-config по SIGPIPE.
	[[ $(dpkg-query -W -f='${Status}' unattended-upgrades 2>/dev/null) == *"install ok installed"* ]] && installed=yes
	[[ $(apt-config dump 2>/dev/null) == *'APT::Periodic::Unattended-Upgrade "1"'* ]] && enabled=yes
	if [[ $installed != yes || $enabled != yes ]]; then
		finding high "Автоматические обновления безопасности выключены" \
			"Известные уязвимости остаются открытыми месяцами, а боты атакуют их автоматически." \
			"apt install unattended-upgrades, затем dpkg-reconfigure -plow unattended-upgrades и выбрать Yes." \
			"пакет установлен: ${installed}, включён: ${enabled}"
	fi

	# apt update не запускаем: он меняет кэш. Список может быть устаревшим.
	local sec
	sec=$(apt list --upgradable 2>/dev/null | grep -c -- '-security')
	if ((sec > 0)); then
		# Свежий сервер всегда отстаёт от зеркала: если автообновления включены и
		# запускались недавно, они сами догонят, и паниковать не о чем.
		local stamp=/var/lib/apt/periodic/unattended-upgrades-stamp age=""
		[[ -f $stamp ]] && age=$((($(date +%s) - $(stat -c %Y "$stamp")) / 86400))
		if [[ $enabled == yes && ( -z $age || $age -le 3 ) ]]; then
			finding low "Есть неустановленные обновления безопасности" \
				"Пока они не установлены, известные уязвимости открыты. Автообновления включены и поставят их сами, обычно в течение суток." \
				"Ничего не делать или поставить сразу: apt update && apt upgrade." \
				"пакетов: ${sec}, автообновления ${age:+последний запуск ${age} дн. назад}${age:-ещё не запускались}"
		else
			finding high "Не установлены обновления безопасности" \
				"В установленных пакетах есть известные уязвимости, для которых уже вышли исправления." \
				"apt update && apt upgrade, затем перезагрузить сервер в спокойное время. Если автообновления включены, проверить, почему они не работают: journalctl -u unattended-upgrades." \
				"пакетов с обновлениями безопасности: ${sec} (по данным последнего apt update)${age:+, автообновления не запускались ${age} дн.}"
		fi
	fi
	if [[ -f /var/run/reboot-required ]]; then
		finding high "Сервер ждёт перезагрузки после обновлений" \
			"Обновлённое ядро и библиотеки не работают до перезагрузки, и уязвимость фактически остаётся открытой." \
			"Перезагрузить сервер в спокойное время. Перед этим проверить, что контейнеры поднимаются сами (restart: unless-stopped)." \
			"/var/run/reboot-required"
	fi
}

check_accounts() {
	checked
	local extra_root
	extra_root=$(awk -F: '$3==0 && $1!="root"{print $1}' /etc/passwd | paste -sd' ' -)
	if [[ -n $extra_root ]]; then
		finding crit "Есть пользователи с правами root, кроме самого root" \
			"Так часто выглядит закладка, оставленная после взлома." \
			"Выяснить, кто и зачем создал этих пользователей. Если не знаете, удалить или поменять им UID." \
			"$extra_root"
	fi

	local admins
	admins=$(getent group sudo wheel admin 2>/dev/null | cut -d: -f4 | tr ',' '\n' | sed '/^$/d' | sort -u | paste -sd' ' -)
	[[ -n $admins ]] && ask "Пользователи с sudo: ${admins}. Вы знаете их всех?"

	if ! is_root; then
		skip "Ключи SSH" "чтобы прочитать authorized_keys всех пользователей, нужны права root"
		return
	fi
	local home f keys
	for home in /root /home/*; do
		for f in "$home/.ssh/authorized_keys" "$home/.ssh/authorized_keys2"; do
			[[ -s $f ]] || continue
			keys=$(ssh-keygen -lf "$f" 2>/dev/null | mask_emails | sed 's/^/     /')
			[[ -n $keys ]] && ask "Ключи для входа в ${f}:"$'\n'"${keys}"$'\n'"   Вы узнаёте все эти ключи? Чужой ключ означает, что у кого-то есть постоянный доступ к серверу."
		done
	done
}

check_secret_files() {
	if ! is_root; then
		skip "Права на файлы с секретами" "нужны права root, чтобы смотреть каталоги всех пользователей"
		return
	fi
	checked
	local dirs=() d
	for d in /root /home /opt /srv /var/www /app; do
		[[ -d $d ]] && dirs+=("$d")
	done
	((${#dirs[@]})) || return
	local files
	files=$(find "${dirs[@]}" -maxdepth 6 \
		\( -name node_modules -o -name .git -o -name vendor -o -name .cache -o -name venv -o -name .venv \) -prune -o \
		-type f \( -name '.env' -o -name '.env.*' -o -name '*.pem' -o -name '*.key' -o -name 'id_rsa' -o -name 'id_ed25519' -o -name 'id_ecdsa' \) \
		! -name '*.example' ! -name '*.sample' ! -name '*.template' ! -name '*.dist' \
		-perm -o=r -print 2>/dev/null | head -20)
	if [[ -n $files ]]; then
		finding high "Файлы с секретами доступны на чтение всем пользователям" \
			"Любой процесс на сервере, включая взломанный, прочитает ключи API, пароли от базы и приватные ключи." \
			"chmod 600 для каждого файла, владельцем сделать пользователя, от которого работает приложение." \
			$'\n'"$(sed 's/^/     /' <<<"$files")"
	fi
}

check_dev_servers() {
	checked
	local procs
	procs=$(ps -eo args= 2>/dev/null | grep -E \
		'next dev|vite( |$)|(npm|yarn|pnpm|bun) (run )?dev|nodemon|webpack-dev-server|react-scripts start|ng serve|nuxt dev|manage\.py runserver|flask run|uvicorn.*--reload' \
		| grep -v -E 'grep|vite (build|preview)' | cut -c1-120 | sort -u | head -10)
	if [[ -n $procs ]]; then
		finding high "Приложение запущено в режиме разработки" \
			"Dev-сервер медленнее, при ошибке показывает посетителям исходный код и пути к файлам, а после перезагрузки сервера сам не запустится." \
			"Собрать приложение (npm run build) и запускать production-командой (npm start, gunicorn, uvicorn без --reload) в Docker или через systemd." \
			$'\n'"$(sed 's/^/     /' <<<"$procs")"
	fi
}

check_backups() {
	checked
	local dbs
	dbs=$(ps -eo comm= 2>/dev/null | grep -E '^(postgres|mysqld|mariadbd|mongod)$' | sort -u | tr '\n' ' ')
	if has docker && run docker info >/dev/null 2>&1; then
		dbs+=$(run docker ps --format '{{.Image}}' 2>/dev/null | grep -Eo 'postgres|mysql|mariadb|mongo|supabase' | sort -u | tr '\n' ' ')
	fi
	dbs=$(tr ' ' '\n' <<<"$dbs" | sed '/^$/d' | sort -u | paste -sd' ' -)
	if [[ -z ${dbs// /} ]]; then
		ask "Базу данных на сервере не нашли. Если она во внешнем сервисе (Supabase, облачная БД хостера), бэкапы настраиваются там. Знаете, есть ли они?"
		return
	fi

	local sched=""
	local cronfiles=(/etc/crontab /etc/cron.d/* /etc/cron.daily/* /var/spool/cron/crontabs/* /var/spool/cron/*)
	# Системные задачи вроде dpkg-db-backup бэкапят пакетную базу, а не данные.
	local system_jobs='dpkg|apt|man-db|logrotate|popularity-contest'
	sched=$(grep -lEi 'pg_dump|pg_basebackup|mysqldump|mariadb-dump|mongodump|restic|borg|rclone|duplicity|kopia|backup' "${cronfiles[@]}" 2>/dev/null |
		grep -Ev "$system_jobs" | head -5 | paste -sd' ' -)
	if has systemctl; then
		sched+=$(systemctl list-timers --all --no-legend 2>/dev/null | grep -Eio '[a-z0-9@_.-]*(backup|dump|restic|borg)[a-z0-9@_.-]*\.timer' |
			grep -Ev "$system_jobs" | sort -u | paste -sd' ' -)
	fi

	local latest="" dirs=() d
	for d in /root /home /opt /srv /var/backups /backup /backups; do
		[[ -d $d ]] && dirs+=("$d")
	done
	if ((${#dirs[@]})); then
		latest=$(find "${dirs[@]}" -maxdepth 5 -type f \( -name '*.sql' -o -name '*.sql.gz' -o -name '*.dump' -o -name '*.archive' -o -name '*.bson' \) \
			-printf '%T@ %TY-%Tm-%Td %p\n' 2>/dev/null | sort -rn | head -1)
	fi

	if [[ -z ${sched// /} && -z $latest ]]; then
		local note=""
		is_root || note=" Без root видны не все места, запустите через sudo."
		finding high "Не нашли признаков бэкапа базы" \
			"Одна ошибка (ваша, вашего ИИ или сбой диска), и данные пользователей потеряны без возможности восстановить." \
			"Настроить ежедневный дамп базы (pg_dump или mysqldump по cron) и копировать его вне этого сервера, например в S3-хранилище хостера. Если бэкапы делает хостер или они лежат в необычном месте, отметьте пункт как принятый риск.${note}" \
			"базы: ${dbs}"
		return
	fi
	if [[ -n $latest ]]; then
		local ts date path age
		read -r ts date path <<<"$latest"
		age=$((($(date +%s) - ${ts%.*}) / 86400))
		if ((age > 7)); then
			finding high "Последний найденный дамп базы старый" \
				"Если что-то случится сейчас, потеряются данные за ${age} дн." \
				"Проверить, что бэкап по расписанию работает, и запускать его хотя бы раз в сутки." \
				"${path} от ${date}"
		fi
	fi
	ask "Бэкапы нашли (${sched:-по расписанию не видно}${latest:+; последний файл: ${latest#* }}). Копия хранится где-то кроме этого сервера? Если сервер пропадёт, вместе с ним пропадут и бэкапы."
}

check_resources() {
	checked
	local use mount
	while read -r use mount; do
		use=${use%\%}
		[[ $use =~ ^[0-9]+$ ]] || continue
		if ((use >= 95)); then
			finding crit "Диск почти заполнен (${mount})" \
				"Когда место кончится, база перестанет писать данные, а приложение и вход по SSH могут сломаться." \
				"Найти, что занимает место: du -xh / --max-depth=2 | sort -h | tail. Частые причины: логи Docker, старые образы (docker system prune) и дампы." \
				"занято ${use}%"
		elif ((use >= 85)); then
			finding high "Диск заполнен больше чем на 85% (${mount})" \
				"Когда место кончится, база перестанет писать данные, а приложение и вход по SSH могут сломаться." \
				"Найти, что занимает место: du -xh / --max-depth=2 | sort -h | tail. Частые причины: логи Docker, старые образы (docker system prune) и дампы." \
				"занято ${use}%"
		fi
	done < <(df -P -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null | awk 'NR>1{print $5, $6}')

	local mem swap
	mem=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo 2>/dev/null)
	swap=$(awk '/^SwapTotal:/{print int($2/1024)}' /proc/meminfo 2>/dev/null)
	if [[ -n $mem && -n $swap ]] && ((swap == 0 && mem < 4096)); then
		finding low "Нет swap на сервере с ${mem} МБ памяти" \
			"При пике нагрузки или сборке приложения память кончается, и система убивает процессы: чаще всего базу или само приложение." \
			"Создать swap-файл на 1–2 ГБ (fallocate, mkswap, swapon и строка в /etc/fstab)."
	fi

	local ooms=""
	if has journalctl; then
		ooms=$(journalctl -k --since "-7 days" --no-pager 2>/dev/null | grep -ci 'out of memory')
	fi
	if [[ -n $ooms ]] && ((ooms > 0)); then
		finding high "За неделю система убивала процессы из-за нехватки памяти" \
			"Приложение или база падают в случайные моменты, пользователи видят ошибки." \
			"Добавить swap, ограничить память контейнеров (mem_limit) или взять тариф с большей памятью." \
			"случаев за 7 дней: ${ooms}"
	fi
}

# Секрет в отчёте показываем только началом: отчёт пересылают.
mask() { printf '%s…(%d симв.)' "${1:0:10}" "${#1}"; }

existing_dirs() {
	local d
	for d in "$@"; do
		[[ -d $d ]] && echo "$d"
	done
}

load_nginx() {
	has nginx && is_root || return
	# Без комментариев: в стандартном nginx.conf есть «# server_tokens off;».
	NGINX_CONF=$(nginx -T 2>/dev/null | sed 's/#.*//')
}

check_web_roots() {
	if [[ -z $NGINX_CONF ]]; then
		skip "Публичные папки сайта" "nginx не найден или нет прав root"
		return
	fi
	checked
	# Правило deny для скрытых файлов действует только в своём блоке server, поэтому
	# смотрим каждый блок отдельно: папка открыта, если хоть один блок отдаёт её без правила.
	local roots
	roots=$(awk '
		/^[[:space:]]*server[[:space:]]*\{/ && !in_server { in_server = 1; depth = 0; root = ""; deny = 0 }
		in_server {
			if ($1 == "root" && root == "") { root = $2; gsub(";", "", root) }
			if (index($0, "location ~ /\\.")) deny = 1
			depth += gsub(/\{/, "{") - gsub(/\}/, "}")
			if (depth == 0) { if (root != "") print root, deny; in_server = 0 }
		}' <<<"$NGINX_CONF" | sort -k1,1 -k2,2n | awk '!seen[$1]++')

	local root deny hits=() files
	while read -r root deny; do
		[[ -d $root ]] || continue
		if [[ $deny == 1 ]]; then
			files=$(find "$root" -maxdepth 4 -name node_modules -prune -o -type f \
				\( -name '*.sql' -o -name '*.sql.gz' -o -name '*.dump' -o -name '*.sqlite' -o -name '*.db' -o -name '*.bak' -o -name 'docker-compose*.yml' \) -print 2>/dev/null)
		else
			files=$(find "$root" -maxdepth 4 -name node_modules -prune -o \
				\( -type d -name .git -o -type f \( -name '.env' -o -name '.env.*' -o -name '*.sql' -o -name '*.sql.gz' -o -name '*.dump' -o -name '*.sqlite' -o -name '*.db' -o -name '*.bak' -o -name 'docker-compose*.yml' \) \) -print 2>/dev/null)
		fi
		[[ -n $files ]] && hits+=("$files")
	done <<<"$roots"

	if ((${#hits[@]})); then
		finding crit "Файлы с паролями или данными лежат в публичной папке сайта" \
			"nginx отдаёт их любому, кто наберёт адрес вида /.env или /backup.sql. Боты проверяют такие адреса на всех сайтах подряд." \
			"Перенести эти файлы из папки сайта. Ключи из попавшего туда .env считать утёкшими и выпустить новые. Добавить в server {} блок nginx: location ~ /\\. { deny all; }" \
			$'\n'"$(printf '%s\n' "${hits[@]}" | head -15 | sed 's/^/     /')"
	fi
}

check_nginx_config() {
	[[ -n $NGINX_CONF ]] || return
	checked
	local has_ssl=no
	grep -Eq '^[[:space:]]*ssl_certificate[[:space:]]' <<<"$NGINX_CONF" && has_ssl=yes

	if [[ $has_ssl == no ]]; then
		local names
		names=$(awk '$1=="server_name"{for(i=2;i<=NF;i++){gsub(";","",$i); if($i!="_" && $i!="localhost") print $i}}' <<<"$NGINX_CONF" | sort -u | paste -sd' ' -)
		if [[ -n $names ]]; then
			finding high "Сайт работает без HTTPS" \
				"Пароли, cookies и данные пользователей идут по сети открытым текстом. Браузер помечает сайт как «Не защищён»." \
				"Получить бесплатный сертификат: apt install certbot python3-certbot-nginx, затем certbot --nginx -d ${names%% *}. Certbot сам настроит перенаправление с HTTP." \
				"в nginx нет ssl_certificate, сайты: ${names}"
		fi
	elif ! grep -Eq 'return[[:space:]]+30[178][[:space:]]+https|rewrite[^;]*https://' <<<"$NGINX_CONF"; then
		finding low "Не нашли перенаправления с HTTP на HTTPS" \
			"Кто откроет сайт по старой ссылке с http://, будет работать без шифрования." \
			"В server {} для порта 80 оставить return 301 https://\$host\$request_uri; Если перенаправление делает приложение или хостер, пункт можно принять как риск."
	fi

	local missing=()
	grep -qi 'add_header[[:space:]]\+X-Content-Type-Options' <<<"$NGINX_CONF" || missing+=("X-Content-Type-Options")
	grep -Eqi 'add_header[[:space:]]+(X-Frame-Options|Content-Security-Policy)' <<<"$NGINX_CONF" || missing+=("X-Frame-Options")
	if [[ $has_ssl == yes ]]; then
		grep -qi 'add_header[[:space:]]\+Strict-Transport-Security' <<<"$NGINX_CONF" || missing+=("Strict-Transport-Security")
	fi
	if ((${#missing[@]})); then
		finding low "В nginx не заданы заголовки безопасности" \
			"Браузер не получает подсказок, как защищать посетителей: например, сайт можно встроить в чужую страницу. Если заголовки ставит само приложение, пункт можно принять как риск." \
			"Добавить в server {}: add_header X-Content-Type-Options nosniff always; add_header X-Frame-Options SAMEORIGIN always; а после настройки HTTPS — Strict-Transport-Security." \
			"$(printf '%s ' "${missing[@]}")"
	fi
	if ! grep -Eq 'server_tokens[[:space:]]+off' <<<"$NGINX_CONF"; then
		finding low "nginx сообщает свою версию" \
			"Ботам проще подобрать известную уязвимость под конкретную версию." \
			"В /etc/nginx/nginx.conf в блоке http раскомментировать или добавить server_tokens off;"
	fi
}

check_certs() {
	if ! is_root || ! has openssl; then
		skip "Сертификаты HTTPS" "нужны права root и openssl"
		return
	fi
	local certs
	certs=$( {
		awk '$1=="ssl_certificate"{gsub(";","",$2); print $2}' <<<"$NGINX_CONF"
		ls /etc/letsencrypt/live/*/cert.pem 2>/dev/null
	} | sort -u)
	[[ -n $certs ]] || return
	checked

	local cert subj issuer end
	while read -r cert; do
		[[ -r $cert ]] || continue
		subj=$(openssl x509 -noout -subject -in "$cert" 2>/dev/null | sed 's/^subject= *//')
		issuer=$(openssl x509 -noout -issuer -in "$cert" 2>/dev/null | sed 's/^issuer= *//')
		end=$(openssl x509 -noout -enddate -in "$cert" 2>/dev/null | cut -d= -f2)
		if ! openssl x509 -noout -checkend $((3 * 86400)) -in "$cert" >/dev/null 2>&1; then
			finding crit "Сертификат HTTPS истекает меньше чем через 3 дня или уже истёк" \
				"Браузеры перестанут открывать сайт без страшного предупреждения." \
				"Продлить: certbot renew, затем systemctl reload nginx." \
				"${cert}, действует до ${end}"
		elif ! openssl x509 -noout -checkend $((14 * 86400)) -in "$cert" >/dev/null 2>&1; then
			finding high "Сертификат HTTPS истекает меньше чем через 14 дней" \
				"Если автопродление не сработает, сайт перестанет нормально открываться." \
				"Проверить автопродление: certbot renew --dry-run." \
				"${cert}, действует до ${end}"
		fi
		if [[ -n $subj && $subj == "$issuer" ]]; then
			finding high "Самоподписанный сертификат HTTPS" \
				"Браузер показывает посетителям предупреждение об опасном сайте, многие сразу уходят." \
				"Выпустить бесплатный сертификат Let's Encrypt: certbot --nginx -d ваш-домен." \
				"$cert"
		fi
	done <<<"$certs"

	if ls /etc/letsencrypt/live/*/cert.pem >/dev/null 2>&1; then
		local renew=no
		systemctl is-active --quiet certbot.timer 2>/dev/null && renew=yes
		systemctl is-active --quiet snap.certbot.renew.timer 2>/dev/null && renew=yes
		grep -qs certbot /etc/crontab /etc/cron.d/* && renew=yes
		if [[ $renew == no ]]; then
			finding high "Автопродление сертификатов не настроено" \
				"Сертификат Let's Encrypt живёт 90 дней. Без продления сайт однажды перестанет открываться." \
				"Включить таймер: systemctl enable --now certbot.timer, затем проверить: certbot renew --dry-run."
		fi
	fi
}

check_frontend_secrets() {
	checked
	local dirs roots=()
	mapfile -t roots < <(existing_dirs "${PROJECT_DIRS[@]}")
	((${#roots[@]})) || return
	dirs=$(find "${roots[@]}" -maxdepth 6 -name node_modules -prune -o -type d \
		\( -path '*/.next/static' -o -name dist -o -name build -o -name out \) -print 2>/dev/null | head -30)
	if [[ -z $dirs ]]; then
		is_root || skip "Ключи в коде для браузера" "без root видны не все каталоги проектов"
		return
	fi

	local re='sk-ant-[A-Za-z0-9_-]{20,}|sk-(proj|svcacct|or-v1)-[A-Za-z0-9_-]{20,}|sk_live_[0-9A-Za-z]{20,}|rk_live_[0-9A-Za-z]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{30,}|[0-9]{8,10}:AA[0-9A-Za-z_-]{33}|AQVN[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'
	local hits=() file match payload
	while IFS=: read -r file match; do
		[[ -z $match ]] && continue
		if [[ $match == eyJ* ]]; then
			# Ключ anon в Supabase публичный по замыслу, опасен только service_role.
			payload=$(cut -d. -f2 <<<"$match" | tr '_-' '/+')
			while ((${#payload} % 4)); do payload+="="; done
			[[ $(base64 -d <<<"$payload" 2>/dev/null) == *service_role* ]] || continue
			hits+=("ключ service_role Supabase $(mask "$match") в ${file}")
			continue
		fi
		hits+=("$(mask "$match") в ${file}")
	done < <(while read -r d; do
		grep -rEoI --include='*.js' --include='*.mjs' --include='*.html' "$re" "$d" 2>/dev/null
	done <<<"$dirs" | sort -u | head -20)

	if ((${#hits[@]})); then
		finding crit "Секретные ключи попали в код, который скачивает браузер" \
			"Любой посетитель откроет исходный код страницы и заберёт ключ: потратит ваши деньги на OpenAI, прочитает базу через service_role или спишет оплату через Stripe." \
			"Отозвать эти ключи у провайдера и выпустить новые. Перенести запросы с ключом на сервер (API-маршрут), а в браузер отдавать только результат." \
			$'\n'"$(printf '     %s\n' "${hits[@]}")"
	fi
}

check_public_env_names() {
	checked
	local files names roots=()
	mapfile -t roots < <(existing_dirs "${PROJECT_DIRS[@]}")
	((${#roots[@]})) || return
	files=$(find "${roots[@]}" -maxdepth 6 -name node_modules -prune -o -type f \
		\( -name '.env' -o -name '.env.*' \) ! -name '*.example' ! -name '*.sample' -print 2>/dev/null | head -30)
	[[ -n $files ]] || return
	# Только имена переменных: значения в отчёт не попадают.
	names=$(while read -r f; do
		grep -Eo '^(NEXT_PUBLIC_|VITE_|REACT_APP_|EXPO_PUBLIC_|NUXT_PUBLIC_)[A-Z0-9_]*(SECRET|SERVICE_ROLE|PRIVATE|PASSWORD|OPENAI|ANTHROPIC|STRIPE_SECRET|API_KEY)[A-Z0-9_]*' "$f" 2>/dev/null | sed "s|\$| (${f})|"
	done <<<"$files" | sort -u | head -15)
	if [[ -n $names ]]; then
		finding high "Секретные переменные объявлены как публичные" \
			"Переменные с префиксом NEXT_PUBLIC_, VITE_ и похожими сборщик вставляет прямо в код для браузера. Ключ из такой переменной видит любой посетитель." \
			"Переименовать без публичного префикса и использовать только в серверном коде. Ключ, который уже побывал в собранном фронтенде, перевыпустить. Публичные по замыслу ключи (карты, аналитика, anon-ключ Supabase) можно оставить." \
			$'\n'"$(sed 's/^/     /' <<<"$names")"
	fi
}

print_section() {
	local color=$1 title=$2
	shift 2
	(($#)) || return
	printf '\n%s%s%s %s(%d)%s\n' "$color$C_B" "$title" "$C_0" "$C_DIM" "$#" "$C_0"
	local item n=1
	for item in "$@"; do
		printf '\n%d. %s\n' "$n" "$item"
		n=$((n + 1))
	done
}

main() {
	local os="неизвестная ОС"
	[[ -r /etc/os-release ]] && os=$(. /etc/os-release && echo "${PRETTY_NAME:-$ID}")

	printf '%sПроверка сервера%s, checkup.sh %s\n' "$C_B" "$C_0" "$VERSION"
	printf '%s · ядро %s · %s\n' "$os" "$(uname -r)" "$(date '+%Y-%m-%d %H:%M %Z')"
	is_root || printf '%sЗапущено без root: часть пунктов не проверяется. Для полной проверки: sudo bash checkup.sh%s\n' "$C_YEL" "$C_0"
	printf '%sСкрипт только читает состояние сервера и ничего не меняет.%s\n' "$C_DIM" "$C_0"

	load_ufw
	load_nginx
	check_ssh
	check_docker
	check_ports
	check_firewall
	check_bruteforce
	check_updates
	check_accounts
	check_secret_files
	check_dev_servers
	check_backups
	check_resources
	check_web_roots
	check_nginx_config
	check_certs
	check_frontend_secrets
	check_public_env_names

	print_section "$C_RED" "КРИТИЧНО — исправить сейчас" "${CRIT[@]}"
	print_section "$C_YEL" "ВАЖНО — исправить в ближайшие дни" "${HIGH[@]}"
	print_section "$C_BLU" "ЖЕЛАТЕЛЬНО" "${LOW[@]}"
	print_section "$C_B" "Нужен ваш ответ" "${ASK[@]}"

	if ((${#PUBLIC_PORTS[@]})); then
		printf '\n%sОстальные порты, открытые наружу%s\n' "$C_B" "$C_0"
		printf '   %s\n' "${PUBLIC_PORTS[@]}"
		printf '   %sЕсли какой-то из них вы не открывали намеренно, его стоит закрыть.%s\n' "$C_DIM" "$C_0"
	fi
	if ((${#FW_CLOSED_PORTS[@]})); then
		printf '\n%sСлушают на всех адресах, но закрыты файрволом ufw%s\n' "$C_B" "$C_0"
		printf '   %s\n' "${FW_CLOSED_PORTS[@]}"
		printf '   %sСейчас снаружи недоступны. Если выключить ufw, станут доступны: надёжнее привязать их к 127.0.0.1.%s\n' "$C_DIM" "$C_0"
	fi
	if ((${#SKIPS[@]})); then
		printf '\n%sНе проверялось%s\n' "$C_DIM" "$C_0"
		printf '   %s\n' "${SKIPS[@]}"
	fi

	printf '\n%sПроверено %d пунктов, найдено проблем: %d (критичных %d, важных %d, желательных %d). Остальное не проверялось.%s\n' \
		"$C_B" "$CHECKED" "$FOUND" "${#CRIT[@]}" "${#HIGH[@]}" "${#LOW[@]}" "$C_0"
}

main "$@"
