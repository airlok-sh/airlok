#!/usr/bin/env bash
# База сервера: обновления, Docker, файрвол, nginx, SSH по ключу.
# Повторный запуск безопасен: каждый шаг сначала смотрит, не сделан ли он уже. Его делают
# install.sh и каждое обновление агента, поэтому настройки человека шаги дополняют, а не
# переписывают: свой Docker, daemon.json, выключенный ufw и свой сайт nginx остаются как есть.
#
#   server-base.sh             — настроить сервер
#   server-base.sh --skip-ssh  — всё, кроме SSH (так запускает install.sh)
#   server-base.sh ssh         — только закрыть вход по паролю, если есть ключ
#   server-base.sh confirm-ssh — подтвердить, что вход по ключу работает, и отменить откат
#   server-base.sh ssh-open    — вернуть вход по паролю, который закрыл airlok
#   server-base.sh letsencrypt [EMAIL] — выпускать для сайтов сертификаты Let's Encrypt
#
# AIRLOK_FIREWALL=off — при первой установке не включать ufw (опция --no-firewall установщика).

source "$(dirname "$0")/lib.sh"

SSH_DROPIN=/etc/ssh/sshd_config.d/00-airlok.conf
SSH_ROLLBACK_UNIT=airlok-ssh-rollback
# Агенту нужно окно дольше: человек идёт в терминал по подсказке из дашборда.
SSH_ROLLBACK_SECONDS=${AIRLOK_SSH_ROLLBACK_SECONDS:-180}
[[ $SSH_ROLLBACK_SECONDS =~ ^[1-9][0-9]{1,3}$ ]] || die "AIRLOK_SSH_ROLLBACK_SECONDS — число секунд от 10 до 9999"
APT_LOG=/var/log/airlok-apt.log
APT_LOCK_WAIT=${AIRLOK_APT_LOCK_WAIT:-600}

# Сервер уже готовили раньше: так повторный запуск узнаётся без новых флагов, ведь update.sh
# прежней версии вызывает server-base.sh по-старому.
installed_before() {
	[[ -e /etc/nginx/snippets/airlok-security-headers.conf || -e $DASHBOARD_CONF || -e $SERVER_CONF ]]
}

preflight() {
	step "Предварительная проверка"
	local os mem disk
	os=$(. /etc/os-release && echo "${ID}-${VERSION_ID}")
	[[ $os == ubuntu-24.04 || $os == ubuntu-26.04 ]] || die "рецепт проверен только на Ubuntu 24.04 и 26.04, здесь ${os}"
	disk=$(df -Pm / | awk 'NR==2{print $4}')
	# Обновление агента не должно падать оттого, что сервер с тех пор обжили: место кончается,
	# а на 443 встал VPN.
	if [[ $FIRST_RUN == no ]]; then
		((disk >= 5000)) || note "на диске свободно ${disk} МБ: сборкам и копиям базы может не хватить места"
		ok "Ubuntu ${os#ubuntu-}, ${disk} МБ на диске"
		return
	fi
	mem=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo)
	((mem >= 700)) || die "нужно хотя бы 1 ГБ памяти, здесь ${mem} МБ"
	((disk >= 5000)) || die "нужно хотя бы 5 ГБ свободного места, здесь ${disk} МБ"
	local busy
	busy=$(ss -H -tlnp '( sport = :80 or sport = :443 )' | grep -v nginx || true)
	[[ -z $busy ]] || die "порты 80/443 заняты не nginx: ${busy}"
	ok "Ubuntu ${os#ubuntu-}, ${mem} МБ памяти, ${disk} МБ на диске"
}

# apt_install ЧТО ПАКЕТ… — на только что арендованном сервере apt часто занят автообновлениями.
# Проверка не исключает гонку: apt могут занять сразу после неё, а его DPkg::Lock::Timeout на
# блокировку списков пакетов не действует. Поэтому на занятый apt отвечаем новой попыткой.
apt_install() {
	local what=$1 deadline=$((SECONDS + APT_LOCK_WAIT)) busy
	shift
	busy="система занята другой установкой уже $((APT_LOCK_WAIT / 60)) мин (часто это автообновления). Ничего не сломалось: подождите несколько минут и запустите команду ещё раз"
	while :; do
		python3 "$RECIPES_DIR/osupdates.py" wait-lock "$((deadline - SECONDS))" || [[ $? != 3 ]] || die "$busy"
		{ apt-get update -qq && apt-get install -y -qq "$@"; } >"$APT_LOG" 2>&1 && return 0
		if ! grep -qE 'Could not get lock|Unable to acquire the dpkg frontend lock' "$APT_LOG"; then
			tail -20 "$APT_LOG" >&2
			die "не удалось установить ${what}, полный лог: ${APT_LOG}"
		fi
		((SECONDS < deadline)) || die "$busy"
		sleep 5
	done
}

pkg_installed() { [[ $(dpkg-query -W -f='${db:Status-Abbrev}' "$1" 2>/dev/null) == ii* ]]; }
apt_candidate() { [[ $(apt-cache policy "$1" 2>/dev/null) =~ Candidate:\ [^\(] ]]; }

# docker_packages — какие пакеты Docker ставить (DOCKER_PKGS) и что о нём сказать (DOCKER_NOTE).
# Второй Docker рядом с уже стоящим не ставим: docker.io из Ubuntu удалил бы docker-ce и
# containerd.io с download.docker.com.
docker_packages() {
	DOCKER_PKGS=() DOCKER_NOTE=docker
	if pkg_installed docker-ce; then
		DOCKER_NOTE="Docker CE оставлен как был"
		if ! docker compose version >/dev/null 2>&1; then
			apt_candidate docker-compose-plugin || die "у Docker CE нет docker compose, а пакета docker-compose-plugin в источниках apt не нашлось: поставьте его и запустите команду ещё раз"
			DOCKER_PKGS+=(docker-compose-plugin)
		fi
		# Без buildx сборка по Dockerfile идёт старым сборщиком, поэтому его отсутствие не ошибка.
		if ! docker buildx version >/dev/null 2>&1 && apt_candidate docker-buildx-plugin; then
			DOCKER_PKGS+=(docker-buildx-plugin)
		fi
		return
	fi
	# snap смотрим до command -v docker: под sudo /snap/bin есть в PATH, и snap-версию нашло бы как «свой» Docker.
	if [[ -e /snap/bin/docker ]] && ! pkg_installed docker.io; then
		die "Docker стоит из snap, а Airlok работает с Docker из apt. Второй Docker рядом ставить не будем: перенесите контейнеры и уберите snap-версию (sudo snap remove docker) или возьмите чистый сервер"
	fi
	if pkg_installed docker.io || ! command -v docker >/dev/null; then
		# Рядом с уже стоящим docker.io snap-версия ничего нового не добавляет, и ночное обновление
		# агента на таком сервере останавливать незачем.
		if [[ -e /snap/bin/docker ]]; then
			note "Рядом с Docker из apt стоит Docker из snap: Airlok работает с первым, snap-версию лучше убрать (sudo snap remove docker)"
		fi
		DOCKER_PKGS=(docker.io docker-compose-v2 docker-buildx)
		return
	fi
	docker compose version >/dev/null 2>&1 || die "Docker на сервере поставлен не из пакетов docker.io или docker-ce и без docker compose. Второй Docker рядом ставить не будем: добавьте к нему docker compose или возьмите чистый сервер"
	DOCKER_NOTE="Docker оставлен как был"
}

packages() {
	step "Пакеты и обновления"
	docker_packages
	apt_install пакеты "${DOCKER_PKGS[@]}" nginx ufw fail2ban \
		unattended-upgrades python3-yaml openssl rsync
	ok "${DOCKER_NOTE}, nginx, ufw, fail2ban установлены"

	if write_if_changed /etc/apt/apt.conf.d/20auto-upgrades 0644 <<'EOF'; then
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
		ok "автообновления безопасности включены"
	else
		ok "автообновления безопасности уже включены"
	fi
}

swap() {
	step "Swap"
	local mem swap
	mem=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo)
	swap=$(awk '/^SwapTotal:/{print int($2/1024)}' /proc/meminfo)
	if ((swap > 0 || mem >= 4096)); then
		ok "не нужен или уже есть (${swap} МБ)"
		return
	fi
	fallocate -l 2G /swapfile
	chmod 600 /swapfile
	mkswap -q /swapfile
	swapon /swapfile
	grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
	ok "создан swap 2 ГБ"
}

# docker_daemon дописывает в daemon.json ротацию логов и live-restore, только если их не задали
# сами: data-root, зеркала и прочие ключи человека остаются. Битый файл не трогаем.
docker_daemon() {
	step "Docker"
	local conf=/etc/docker/daemon.json backup="" added="" rc=0 containers args=()
	args+=("--dockerd-flags=$(systemctl show docker.service -p ExecStart --value 2>/dev/null || true)")
	[[ $(docker info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null) == active ]] && args+=(--swarm)
	containers=$(docker ps -aq 2>/dev/null) || containers=unknown
	# Запрет повышать права для всех контейнеров — только на чистом сервере: он закрывает и шаги RUN
	# при сборке по Dockerfile, но сломал бы чужие контейнеры с sudo или setuid. Свои контейнеры
	# Airlok и так запускает с no-new-privileges.
	if [[ $FIRST_RUN == yes && ! -s $conf && -z $containers ]]; then
		args+=(--no-new-privileges)
	fi
	if [[ -e $conf ]]; then
		backup=$(mktemp)
		cp -p "$conf" "$backup"
	fi
	added=$(python3 "$RECIPES_DIR/serverbase.py" daemon-json "$conf" "${args[@]}") || rc=$?
	case $rc in
	0)
		if ! docker_apply; then
			if [[ -n $backup ]]; then cp -p "$backup" "$conf"; else rm -f "$conf"; fi
			rm -f "$backup"
			systemctl restart docker || true
			die "Docker не запустился с дополненным ${conf}, вернули прежний; подробности: journalctl -u docker"
		fi
		ok "в ${conf} дописано: ${added}; остальные настройки не тронуты"
		;;
	10) ok "настройки демона уже на месте" ;;
	11) note "${conf} не разобрать как JSON — оставили как есть, ротацию логов не добавили" ;;
	*)
		rm -f "$backup"
		die "не удалось дополнить ${conf}"
		;;
	esac
	rm -f "$backup"
	systemctl enable --now docker >/dev/null 2>&1
}

# docker_apply — применить новый daemon.json. Перезапуск Docker без live-restore останавливает
# контейнеры, поэтому при запущенных сначала включаем live-restore перечитыванием настроек: оно
# обходится без остановки, и после него контейнеры переживут перезапуск.
docker_apply() {
	if [[ -n $(docker ps -q 2>/dev/null) ]] && ! live_restore_on; then
		systemctl reload docker || return 1
		local tries=10
		while ((tries--)) && ! live_restore_on; do sleep 1; done
		if ! live_restore_on; then
			note "ротация логов заработает после перезапуска Docker (sudo systemctl restart docker): сейчас его не перезапускали, чтобы не остановить контейнеры"
			return 0
		fi
	fi
	systemctl restart docker
}

live_restore_on() { [[ $(docker info --format '{{.LiveRestoreEnabled}}' 2>/dev/null) == true ]]; }

# firewall включает ufw только при первой установке и открывает в нём всё, что уже слушает снаружи:
# VPN и чужие сервисы на сервере должны работать и дальше. Потом во включённый ufw только добавляются
# SSH, 80 и 443, а выключенный человеком больше не включается. FIREWALL в server.env: airlok — ufw
# включён, off — выключен.
firewall() {
	step "Файрвол"
	local state ports port
	state=$(conf_value "$SERVER_CONF" FIREWALL)
	if [[ $(ufw status 2>/dev/null) == "Status: active"* ]]; then
		ports=$(python3 "$RECIPES_DIR/serverbase.py" firewall-ports)
		for port in $ports; do ufw allow "$port" >/dev/null; done
		[[ -n $state ]] || conf_set "$SERVER_CONF" FIREWALL airlok
		ok "ufw включён, открыты ${ports//$'\n'/, }; остальные правила не тронуты"
		note "контейнеры публикуют порты только на 127.0.0.1, поэтому Docker не обходит ufw"
		return
	fi
	if [[ -n $state || $FIRST_RUN == no || ${AIRLOK_FIREWALL:-} == off ]]; then
		conf_set "$SERVER_CONF" FIREWALL off
		ok "ufw выключен, Airlok его не включает"
		note "включить: сначала разрешите нужные порты (sudo ufw allow ПОРТ/tcp), затем sudo ufw enable"
		return
	fi
	ports=$(python3 "$RECIPES_DIR/serverbase.py" firewall-ports --listening)
	# Разрешаем до включения: иначе ufw enable оборвёт текущее подключение по SSH.
	for port in $ports; do ufw allow "$port" >/dev/null; done
	ufw default deny incoming >/dev/null
	ufw default allow outgoing >/dev/null
	ufw --force enable >/dev/null
	conf_set "$SERVER_CONF" FIREWALL airlok
	ok "ufw включён, открыты ${ports//$'\n'/, }; остальное закрыто"
	note "открыть ещё порт: sudo ufw allow ПОРТ/tcp"
	note "контейнеры публикуют порты только на 127.0.0.1, поэтому Docker не обходит ufw"
}

bruteforce() {
	step "Защита от перебора"
	systemctl enable --now fail2ban >/dev/null 2>&1
	ok "fail2ban включён"
}

web() {
	step "nginx"
	local changed=no
	# В nginx.conf из Ubuntu 26.04 уже стоит server_tokens build, и вторую такую директиву
	# на уровне http nginx не примет. Сам nginx.conf не правим: изменённый пакетный конфиг
	# останавливает автообновления nginx. Версию тогда прячет сниппет в каждом server.
	if grep -qE '^[[:space:]]*server_tokens[[:space:]]' /etc/nginx/nginx.conf; then
		if [[ -e /etc/nginx/conf.d/00-airlok.conf ]]; then
			rm -f /etc/nginx/conf.d/00-airlok.conf
			changed=yes
		fi
	else
		write_if_changed /etc/nginx/conf.d/00-airlok.conf 0644 <<'EOF' && changed=yes
server_tokens off;
EOF
	fi
	ensure_log_format && changed=yes
	write_if_changed /etc/nginx/snippets/airlok-security-headers.conf 0644 <<'EOF' && changed=yes
server_tokens off;
add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options SAMEORIGIN always;
add_header Referrer-Policy strict-origin-when-cross-origin always;
EOF
	local default=""
	if [[ -e /etc/nginx/sites-enabled/default ]]; then
		if default_site_pristine; then
			rm -f /etc/nginx/sites-enabled/default
			changed=yes
			default=", стандартная страница убрана"
		else
			note "сайт nginx по умолчанию (/etc/nginx/sites-enabled/default) изменён или отдаёт ваши файлы — оставили его: запросы по IP сервера и к доменам без своего сайта пойдут в него"
		fi
	fi
	if [[ $changed == yes ]]; then
		nginx -t -q
		systemctl reload nginx
	fi
	systemctl enable --now nginx >/dev/null 2>&1
	ok "версия скрыта, общие заголовки безопасности готовы${default}"
}

# default_site_pristine — сайт по умолчанию такой, каким его поставил пакет nginx, и отдаёт только
# его приветственную страницу: убрать его ничего не сломает.
default_site_pristine() {
	local link=/etc/nginx/sites-enabled/default site=/etc/nginx/sites-available/default www=/var/www/html
	local pkg sum f
	[[ -L $link && $(readlink -f "$link") == "$site" && -f $site ]] || return 1
	pkg=$(dpkg -S "$site" 2>/dev/null) || return 1
	pkg=${pkg%%:*}
	sum=$(dpkg-query -W -f='${Conffiles}\n' "$pkg" 2>/dev/null | awk -v f="$site" '$1 == f {print $2}')
	[[ -n $sum && $(md5sum <"$site" | cut -d' ' -f1) == "$sum" ]] || return 1
	for f in "$www"/* "$www"/.[!.]*; do
		[[ -e $f ]] || continue
		[[ ${f##*/} == index.nginx-debian.html ]] || return 1
	done
}

# letsencrypt [EMAIL] — включить выпуск сертификатов: дальше их выпускает app-deploy.sh
# при выкатке, потому что сертификат привязан к домену приложения.
letsencrypt() {
	local email=${1:-}
	if [[ -n $email ]]; then
		[[ $email =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "это не похоже на email: ${email}"
		conf_set "$SERVER_CONF" LETSENCRYPT_EMAIL "$email"
	fi
	conf_set "$SERVER_CONF" LETSENCRYPT 1
	install_certbot
	note "сертификат выпускается при публикации; A-запись домена приложения должна указывать на этот сервер"
}

install_certbot() {
	step "Let's Encrypt"
	if ! command -v certbot >/dev/null; then
		apt_install certbot certbot
	fi
	systemctl enable --now certbot.timer >/dev/null 2>&1 || true
	ok "certbot установлен, сертификаты продлеваются сами"
}

# Ключи root и того, кто вызвал sudo: под Ubuntu часто входят не root'ом, а ubuntu. Агент
# называет пользователя сам (AIRLOK_SSH_USER) — тот, кому дашборд кладёт ключи устройств.
authorized_keys_present() {
	local keys=(/root/.ssh/authorized_keys) home user
	for user in "${SUDO_USER:-}" "${AIRLOK_SSH_USER:-}"; do
		[[ -n $user && $user != root ]] || continue
		home=$(getent passwd "$user" | cut -d: -f6)
		[[ -n $home ]] && keys+=("$home/.ssh/authorized_keys")
	done
	local f
	for f in "${keys[@]}"; do
		# Перед ключом бывают опции (from=…, command=…), поэтому тип ищем не только в начале строки.
		[[ -s $f ]] && grep -qE '(^|[[:space:]])(ssh-(ed25519|rsa|dss)|ecdsa-sha2-|sk-)' "$f" && return 0
	done
	return 1
}

ssh_hardening() {
	step "SSH"
	if ! authorized_keys_present; then
		note "на сервере нет SSH-ключей (authorized_keys у root${SUDO_USER:+, ${SUDO_USER}}${AIRLOK_SSH_USER:+, ${AIRLOK_SSH_USER}}) — вход по паролю оставлен, иначе доступ к серверу пропадёт"
		return
	fi
	local conf
	conf=$(sshd -T)
	# OpenSSH до 10-й версии (Ubuntu 24.04) печатает prohibit-password старым именем without-password.
	if grep -qx 'passwordauthentication no' <<<"$conf" && grep -qxE 'permitrootlogin (prohibit|without)-password' <<<"$conf"; then
		ok "вход только по ключу уже настроен"
		return
	fi
	# Имя с 00-: sshd берёт первое найденное значение, а хостер кладёт свои
	# PasswordAuthentication yes в 50-cloud-init.conf и подобные файлы.
	cat >"$SSH_DROPIN" <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
	chmod 644 "$SSH_DROPIN"
	if ! sshd -t; then
		rm -f "$SSH_DROPIN"
		die "sshd не принял настройки, вернули как было"
	fi
	systemctl reset-failed "$SSH_ROLLBACK_UNIT" 2>/dev/null || true
	# AccuracySec: без него systemd вправе сдвинуть срабатывание на минуту.
	systemd-run --quiet --unit="$SSH_ROLLBACK_UNIT" --on-active="$SSH_ROLLBACK_SECONDS" \
		--timer-property=AccuracySec=1s \
		/bin/sh -c "rm -f '$SSH_DROPIN' && systemctl reload ssh"
	systemctl reload ssh
	ok "вход по паролю выключен, root входит только по ключу"
	# CLI проверяет вход по ключу сам, новым SSH-соединением, а агент ждёт такой вход в журнале sshd.
	if [[ ${AIRLOK_SSH_CONFIRM:-} == cli || ${AIRLOK_SSH_CONFIRM:-} == agent ]]; then
		printf '  вход по ключу проверит и подтвердит airlok; без подтверждения за %d секунд настройки SSH вернутся сами\n' "$SSH_ROLLBACK_SECONDS"
		return
	fi
	printf '\n  %sПроверьте вход по ключу в НОВОМ окне терминала, не закрывая это.%s\n' "$C_B" "$C_0"
	printf '  Если вход работает, выполните: %s confirm-ssh\n' "$0"
	printf '  Если не подтвердить за %d секунд, настройки SSH вернутся сами.\n' "$SSH_ROLLBACK_SECONDS"
}

confirm_ssh() {
	need_root
	if systemctl stop "${SSH_ROLLBACK_UNIT}.timer" 2>/dev/null; then
		ok "откат SSH отменён, вход только по ключу закреплён"
	else
		note "таймер отката не найден: либо уже подтверждено, либо настройки уже откатились"
	fi
}

# ssh_open убирает настройки airlok: в силе снова то, что задал хостер. Если и у него вход по
# паролю выключен, сообщаем об этом — чужие настройки sshd не трогаем.
ssh_open() {
	step "SSH"
	systemctl stop "${SSH_ROLLBACK_UNIT}.timer" 2>/dev/null || true
	if [[ -e $SSH_DROPIN ]]; then
		rm -f "$SSH_DROPIN"
		sshd -t || die "sshd не принимает свои настройки без файла airlok: проверьте /etc/ssh/sshd_config"
		systemctl reload ssh
	fi
	if sshd -T | grep -qxE '(passwordauthentication|kbdinteractiveauthentication) yes'; then
		ok "вход по паролю открыт"
		return
	fi
	die "вход по паролю выключен в настройках sshd хостера, airlok их не меняет"
}

main() {
	need_root
	FIRST_RUN=yes
	installed_before && FIRST_RUN=no
	preflight
	packages
	swap
	docker_daemon
	firewall
	bruteforce
	web
	if [[ ${1:-} == skip-ssh ]]; then
		# Закрытие пароля требует проверки входа в новом окне, поэтому в автоматической
		# установке это отдельный осознанный шаг, а не часть одной команды.
		step "SSH"
		note "не трогали; закрыть вход по паролю: sudo $0 ssh"
		return
	fi
	ssh_hardening
}

case ${1:-} in
confirm-ssh) confirm_ssh ;;
ssh-open)
	need_root
	ssh_open
	;;
ssh)
	need_root
	ssh_hardening
	;;
letsencrypt)
	need_root
	letsencrypt "${2:-}"
	;;
--skip-ssh) main skip-ssh ;;
"") main ;;
*) die "неизвестная команда: $1" ;;
esac
