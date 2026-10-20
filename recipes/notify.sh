#!/usr/bin/env bash
# Уведомления о выкатках, откатах и «сайт не отвечает»: в Telegram и в дашборд.
#
#   notify.sh setup           — подключить своего бота; токен вводится скрытно и хранится только на сервере
#   notify.sh test            — отправить пробное сообщение
#   notify.sh dashboard АДРЕС — подключить сервер к дашборду; токен агента читается из stdin

source "$(dirname "$0")/lib.sh"

TOKEN_RE='^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$'

install_healthcheck() {
	local changed=no
	write_if_changed /etc/systemd/system/da-healthcheck.service 0644 <<EOF && changed=yes
[Unit]
Description=DeployAssistant: проверка, что сайты отвечают
After=docker.service nginx.service

[Service]
Type=oneshot
ExecStart=${RECIPES_DIR}/healthcheck.sh
EOF
	write_if_changed /etc/systemd/system/da-healthcheck.timer 0644 <<'EOF' && changed=yes
[Unit]
Description=DeployAssistant: проверять сайты каждую минуту

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF
	[[ $changed == yes ]] && systemctl daemon-reload
	systemctl enable --now da-healthcheck.timer >/dev/null 2>&1
	ok "сайты проверяются каждую минуту"
}

# chat_id берём из последнего сообщения боту, чтобы человеку не искать его вручную.
find_chat_id() {
	local token=$1
	curl -sS --max-time 15 -K - <<<"url = \"https://api.telegram.org/bot${token}/getUpdates\"" |
		python3 -c '
import json, sys
data = json.load(sys.stdin)
chats = [u["message"]["chat"]["id"] for u in data.get("result", []) if "message" in u]
print(chats[-1] if chats else "")
'
}

setup() {
	need_root
	[[ -t 0 ]] || die "setup нужно запускать в терминале: токен вводится вручную"
	cat <<'EOF'
1. В Telegram откройте @BotFather, отправьте /newbot и следуйте подсказкам.
   В конце BotFather пришлёт токен вида 123456789:AA...
EOF
	local token chat
	read -rsp "Вставьте токен (он не будет виден на экране): " token
	echo
	[[ $token =~ $TOKEN_RE ]] || die "это не похоже на токен бота"

	echo "2. Откройте своего бота в Telegram, отправьте ему любое сообщение и нажмите Enter здесь."
	read -r _
	chat=$(find_chat_id "$token") || chat=""
	[[ $chat =~ ^-?[0-9]+$ ]] || die "бот не видит сообщений. Напишите ему и запустите setup ещё раз"

	install -d -m 755 /etc/deployassistant
	printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' "$token" "$chat" | write_if_changed "$TELEGRAM_CONF" 0600 || true
	ok "бот подключён, настройки в ${TELEGRAM_CONF} (права 600)"
	install_healthcheck
	notify "🔔 DeployAssistant подключён к серверу $(hostname). Сюда будут приходить выкатки, откаты и сообщения о том, что сайт не отвечает."
	ok "отправили пробное сообщение — проверьте Telegram"
}

dashboard() {
	need_root
	local url=${1:-} token
	[[ $url =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || die "укажите адрес дашборда без пути, например https://dash.example.ru"
	# Токен не аргументом: аргументы остаются в истории оболочки и видны в ps.
	if [[ -t 0 ]]; then
		read -rsp "Токен агента из дашборда: " token
		echo
	else
		read -r token
	fi
	[[ $token =~ ^da_srv_[A-Za-z0-9_-]{20,}$ ]] || die "это не похоже на токен агента (da_srv_…)"

	install -d -m 755 /etc/deployassistant
	local tmp
	tmp=$(mktemp)
	printf 'DA_API_URL=%s\nDA_AGENT_TOKEN=%s\n' "$url" "$token" >"$tmp"
	local code
	code=$(DASHBOARD_CONF=$tmp dashboard_call GET /agent/v1/ping) || code=000
	case $code in
	204) ;;
	401) rm -f "$tmp"; die "дашборд не принял токен" ;;
	*) rm -f "$tmp"; die "дашборд по адресу ${url} не отвечает (код ${code})" ;;
	esac
	install -m 600 "$tmp" "$DASHBOARD_CONF"
	rm -f "$tmp"
	install_healthcheck
	ok "сервер подключён к ${url}: выкатки и доступность сайтов будут видны в дашборде"
}

case ${1:-} in
setup) setup ;;
dashboard) dashboard "${2:-}" ;;
test)
	need_root
	[[ -r $TELEGRAM_CONF ]] || die "бот не подключён: $0 setup"
	notify "🔔 Проверка уведомлений с сервера $(hostname)"
	ok "отправлено"
	;;
install-healthcheck)
	need_root
	install_healthcheck
	;;
*) die "использование: $0 setup|test" ;;
esac
