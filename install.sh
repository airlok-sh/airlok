#!/usr/bin/env bash
# Установка airlok одной командой: рецепты в /opt/airlok, подготовка сервера и, если
# передан адрес дашборда, подключение к нему. Повторный запуск обновляет и досоздаёт.
#
#   echo "ТОКЕН" | sudo bash -c "$(curl -fsSL https://airlok.ru/install.sh)" install https://app.airlok.ru [опции]
#   sudo bash -c "$(curl -fsSL https://airlok.ru/install.sh)" install [опции]   # без дашборда
#
# Опции:
#   --letsencrypt [EMAIL]  выпускать для сайтов сертификаты Let's Encrypt; email — для писем о продлении
#   --ssh-keys-only        в конце закрыть вход по паролю (только если на сервере уже есть SSH-ключ)
#
# Токен приходит через stdin, а не аргументом: аргументы видны в ps и остаются в истории.
# Сам скрипт поэтому передаётся через bash -c, а не через конвейер в bash.
# AIRLOK_REPO — откуда брать рецепты (зеркало или локальный путь) вместо GitHub.

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

REPO=${AIRLOK_REPO:-https://github.com/airlok-sh/airlok}
DIR=/opt/airlok

die() {
	printf 'Ошибка: %s\n' "$*" >&2
	exit 1
}

[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ -r /etc/os-release ]] && [[ $(. /etc/os-release && echo "$ID") == ubuntu ]] || die "airlok ставится на Ubuntu"

dashboard=""
if [[ -n ${1:-} && ${1:-} != --* ]]; then
	dashboard=$1
	shift
fi
letsencrypt=0 le_email="" keys_only=0
while (($#)); do
	case $1 in
	--letsencrypt)
		letsencrypt=1
		if [[ -n ${2:-} && ${2:-} != --* ]]; then
			le_email=$2
			shift
		fi
		;;
	--ssh-keys-only) keys_only=1 ;;
	*) die "неизвестная опция: $1" ;;
	esac
	shift
done
# Проверяем до установки: опечатка в email не должна обнаружиться через десять минут.
if [[ -n $le_email && ! $le_email =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
	die "это не похоже на email: ${le_email}"
fi

token=""
if [[ -n $dashboard ]]; then
	# Токен читаем до apt: дальше stdin может понадобиться другим программам.
	if [[ -t 0 ]]; then
		read -rsp "Токен агента из дашборда: " token </dev/tty
		echo
	else
		read -r token || true
	fi
	[[ -n $token ]] || die "нет токена агента: скопируйте команду из дашборда целиком"
fi

if ! command -v git >/dev/null; then
	echo "→ Ставим git"
	{ apt-get update -qq && apt-get install -y -qq git; } >/dev/null 2>&1 || die "не удалось поставить git: apt-get install git"
fi

if [[ -d $DIR/.git ]]; then
	echo "→ Обновляем рецепты в ${DIR}"
	git -C "$DIR" pull -q --ff-only || die "не удалось обновить ${DIR}: есть локальные правки? git -C ${DIR} status"
else
	echo "→ Скачиваем рецепты в ${DIR}"
	git clone -q --depth 1 "$REPO" "$DIR"
fi

"$DIR/recipes/server-base.sh" --skip-ssh

if ((letsencrypt)); then
	"$DIR/recipes/server-base.sh" letsencrypt ${le_email:+"$le_email"}
fi

if [[ -n $dashboard ]]; then
	printf '%s\n' "$token" | "$DIR/recipes/notify.sh" dashboard "$dashboard"
fi

cat <<EOF

Сервер готов. Дальше:
  выкатить проект:       sudo ${DIR}/recipes/app-deploy.sh /путь/к/проекту
  или из GitHub:         sudo ${DIR}/recipes/github-deploy.sh setup владелец/репозиторий
EOF
if [[ -n $dashboard ]]; then
	echo "  или кнопкой «Выкатить» в дашборде: ${dashboard}"
fi
if ((!letsencrypt)) && ! grep -qx 'LETSENCRYPT=1' /etc/airlok/server.env 2>/dev/null; then
	echo "  настоящий сертификат вместо самоподписанного: sudo ${DIR}/recipes/server-base.sh letsencrypt"
fi

if ((!keys_only)); then
	echo "  закрыть вход по паролю (если входите по ключу): sudo ${DIR}/recipes/server-base.sh ssh"
	exit 0
fi

# Закрываем пароль последним шагом: дальше настройки SSH откатятся через 3 минуты, если
# вход по ключу не подтвердить, и долгая установка не должна съесть это время.
echo
"$DIR/recipes/server-base.sh" ssh
if systemctl is-active --quiet airlok-ssh-rollback.timer; then
	cat <<EOF

  ══════════════════════════════════════════════════════════════════════
   ВАЖНО: вход по паролю выключен. Не закрывайте это окно.
   1. Откройте НОВОЕ окно терминала и зайдите на сервер по ключу.
   2. Получилось — выполните: sudo ${DIR}/recipes/server-base.sh confirm-ssh
   Если не подтвердить за 3 минуты, вход по паролю вернётся сам.
  ══════════════════════════════════════════════════════════════════════
EOF
elif sshd -T 2>/dev/null | grep -qx 'passwordauthentication no'; then
	echo "  Вход по паролю уже был выключен раньше — ничего подтверждать не нужно."
else
	cat <<EOF

  Вход по паролю оставлен: на сервере нет SSH-ключа, и без пароля вы бы потеряли доступ.
  Создайте ключ на своём компьютере (ssh-keygen -t ed25519) и добавьте его на сервер
  (ssh-copy-id root@IP-сервера), затем: sudo ${DIR}/recipes/server-base.sh ssh
EOF
fi
