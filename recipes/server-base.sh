#!/usr/bin/env bash
# База сервера: обновления, Docker, файрвол, nginx, SSH по ключу.
# Повторный запуск безопасен: каждый шаг сначала смотрит, не сделан ли он уже.
#
#   server-base.sh             — настроить сервер
#   server-base.sh --skip-ssh  — всё, кроме SSH (так запускает install.sh)
#   server-base.sh ssh         — только закрыть вход по паролю, если есть ключ
#   server-base.sh confirm-ssh — подтвердить, что вход по ключу работает, и отменить откат
#   server-base.sh ssh-open    — вернуть вход по паролю, который закрыл airlok
#   server-base.sh letsencrypt [EMAIL] — выпускать для сайтов сертификаты Let's Encrypt

source "$(dirname "$0")/lib.sh"

SSH_DROPIN=/etc/ssh/sshd_config.d/00-airlok.conf
SSH_ROLLBACK_UNIT=airlok-ssh-rollback
# Агенту нужно окно дольше: человек идёт в терминал по подсказке из дашборда.
SSH_ROLLBACK_SECONDS=${AIRLOK_SSH_ROLLBACK_SECONDS:-180}
[[ $SSH_ROLLBACK_SECONDS =~ ^[1-9][0-9]{1,3}$ ]] || die "AIRLOK_SSH_ROLLBACK_SECONDS — число секунд от 10 до 9999"

preflight() {
	step "Предварительная проверка"
	local os mem disk
	os=$(. /etc/os-release && echo "${ID}-${VERSION_ID}")
	[[ $os == ubuntu-24.04 || $os == ubuntu-26.04 ]] || die "рецепт проверен только на Ubuntu 24.04 и 26.04, здесь ${os}"
	mem=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo)
	((mem >= 700)) || die "нужно хотя бы 1 ГБ памяти, здесь ${mem} МБ"
	disk=$(df -Pm / | awk 'NR==2{print $4}')
	((disk >= 5000)) || die "нужно хотя бы 5 ГБ свободного места, здесь ${disk} МБ"
	local busy
	busy=$(ss -H -tlnp '( sport = :80 or sport = :443 )' | grep -v nginx || true)
	[[ -z $busy ]] || die "порты 80/443 заняты не nginx: ${busy}"
	ok "Ubuntu ${os#ubuntu-}, ${mem} МБ памяти, ${disk} МБ на диске"
}

packages() {
	step "Пакеты и обновления"
	local log=/var/log/airlok-apt.log
	if ! { apt-get update -qq && apt-get install -y -qq docker.io docker-compose-v2 nginx ufw fail2ban \
		unattended-upgrades python3-yaml openssl rsync; } >"$log" 2>&1; then
		tail -20 "$log" >&2
		die "не удалось установить пакеты, полный лог: ${log}"
	fi
	ok "docker, nginx, ufw, fail2ban установлены"

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

docker_daemon() {
	step "Docker"
	if write_if_changed /etc/docker/daemon.json 0644 <<'EOF'; then
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" },
  "no-new-privileges": true,
  "live-restore": true
}
EOF
		systemctl restart docker
		ok "ротация логов и no-new-privileges для всех контейнеров"
	else
		ok "настройки демона уже на месте"
	fi
	systemctl enable --now docker >/dev/null 2>&1
}

firewall() {
	step "Файрвол"
	local ssh_port
	ssh_port=$(awk '$1=="port"{print $2; exit}' <<<"$(sshd -T 2>/dev/null)")
	ssh_port=${ssh_port:-22}
	# SSH разрешаем до включения: иначе ufw enable оборвёт текущее подключение.
	ufw allow "${ssh_port}/tcp" >/dev/null
	ufw allow 80/tcp >/dev/null
	ufw allow 443/tcp >/dev/null
	ufw default deny incoming >/dev/null
	ufw default allow outgoing >/dev/null
	ufw --force enable >/dev/null
	ok "открыты ${ssh_port}, 80, 443, остальное закрыто"
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
	if [[ -e /etc/nginx/sites-enabled/default ]]; then
		rm -f /etc/nginx/sites-enabled/default
		changed=yes
	fi
	if [[ $changed == yes ]]; then
		nginx -t -q
		systemctl reload nginx
	fi
	systemctl enable --now nginx >/dev/null 2>&1
	ok "версия скрыта, общие заголовки безопасности готовы, стандартная страница убрана"
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
	note "сертификат выпускается при выкатке; A-запись домена приложения должна указывать на этот сервер"
}

install_certbot() {
	step "Let's Encrypt"
	if ! command -v certbot >/dev/null; then
		local log=/var/log/airlok-apt.log
		if ! { apt-get update -qq && apt-get install -y -qq certbot; } >"$log" 2>&1; then
			tail -20 "$log" >&2
			die "не удалось установить certbot, полный лог: ${log}"
		fi
	fi
	systemctl enable --now certbot.timer >/dev/null 2>&1 || true
	ok "certbot установлен, сертификаты продлеваются сами"
}

# Ключи root и того, кто вызвал sudo: под Ubuntu часто входят не root'ом, а ubuntu.
authorized_keys_present() {
	local keys=(/root/.ssh/authorized_keys) home
	if [[ -n ${SUDO_USER:-} && $SUDO_USER != root ]]; then
		home=$(getent passwd "$SUDO_USER" | cut -d: -f6)
		[[ -n $home ]] && keys+=("$home/.ssh/authorized_keys")
	fi
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
		note "на сервере нет SSH-ключей (authorized_keys у root${SUDO_USER:+ и ${SUDO_USER}}) — вход по паролю оставлен, иначе доступ к серверу пропадёт"
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
