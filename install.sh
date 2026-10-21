#!/usr/bin/env bash
# Установка airlok одной командой: рецепты в /opt/airlok, подготовка сервера и, если
# передан адрес дашборда, подключение к нему. Повторный запуск обновляет и досоздаёт.
#
#   echo "ТОКЕН" | sudo bash -c "$(curl -fsSL https://airlok.ru/install.sh)" install https://app.airlok.ru
#   sudo bash -c "$(curl -fsSL https://airlok.ru/install.sh)"            # без дашборда
#
# Токен приходит через stdin, а не аргументом: аргументы видны в ps и остаются в истории.
# Сам скрипт поэтому передаётся через bash -c, а не через конвейер в bash.

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

REPO=https://github.com/airlok-sh/airlok
DIR=/opt/airlok

die() {
	printf 'Ошибка: %s\n' "$*" >&2
	exit 1
}

[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ -r /etc/os-release ]] && [[ $(. /etc/os-release && echo "$ID") == ubuntu ]] || die "airlok ставится на Ubuntu"

dashboard=${1:-}
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

if [[ -n $dashboard ]]; then
	printf '%s\n' "$token" | "$DIR/recipes/notify.sh" dashboard "$dashboard"
fi

cat <<EOF

Сервер готов. Дальше:
  выкатить проект:       sudo ${DIR}/recipes/app-deploy.sh /путь/к/проекту
  или из GitHub:         sudo ${DIR}/recipes/github-deploy.sh setup владелец/репозиторий
  закрыть вход по паролю (если входите по ключу): sudo ${DIR}/recipes/server-base.sh ssh
EOF
