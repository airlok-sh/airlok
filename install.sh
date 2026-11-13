#!/usr/bin/env bash
# Установка airlok одной командой: рецепты в /opt/airlok, подготовка сервера и, если
# передан адрес дашборда, подключение к нему. Повторный запуск обновляет и досоздаёт.
#
#   sudo bash -c "$(curl -fsSL https://airlok.ru/install.sh)" install https://app.airlok.ru [опции]
#   sudo bash -c "$(curl -fsSL https://airlok.ru/install.sh)" install [опции]   # без дашборда
#
# Опции:
#   --letsencrypt [EMAIL]  выпускать для сайтов сертификаты Let's Encrypt; email — для писем о продлении
#   --ssh-keys-only        в конце закрыть вход по паролю (только если на сервере уже есть SSH-ключ)
#   --no-firewall          не включать ufw. Без опции он включается только при первой установке, и в нём
#                          открыты SSH, 80, 443 и все порты, которые уже слушают снаружи
#   --strict               строгий режим: дашборд не получит доступа к серверу (recipes/strict.py)
#   --allow-repo РЕПОЗИТОРИЙ  что можно публиковать в строгом режиме: владелец/имя или https://хост/путь.git
#
# Для строгого режима лучше не запускать скрипт из сети, а прочитать код и поставить именно его:
#   git clone https://github.com/airlok-sh/airlok /opt/airlok && git -C /opt/airlok checkout КОММИТ
#   sudo /opt/airlok/install.sh https://app.airlok.ru --strict --allow-repo владелец/имя
#
# Токен агента скрипт спрашивает сам, не показывая ввод: аргументы видны в ps, а строка команды
# с токеном осталась бы в истории shell. Без терминала (cloud-init, CLI по SSH) токен читается
# из stdin. Сам скрипт поэтому передаётся через bash -c, а не через конвейер в bash.
# AIRLOK_REPO — откуда брать рецепты (зеркало или локальный путь) вместо GitHub.
# AIRLOK_SSH_CONFIRM=cli — установку запустил airlok CLI: вход по ключу он подтвердит сам,
# просить человека открыть второе окно не нужно. Переменная, а не опция: старый install.sh её
# просто не заметит, а неизвестную опцию отверг бы.

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

REPO=${AIRLOK_REPO:-https://github.com/airlok-sh/airlok}
DIR=/opt/airlok
SIGNERS_CONF=/etc/airlok/allowed_signers

# Открытые ключи подписи версий Airlok — те же, что в recipes/allowed_signers. Встроены в скрипт,
# который отдаёт airlok.ru, а не берутся из клона: иначе новая версия из GitHub назначала бы
# ключ себе сама.
release_signers() {
	cat <<'EOF'
EOF
}

die() {
	printf 'Ошибка: %s\n' "$*" >&2
	exit 1
}

# Рецептов ещё нет, поэтому ждать как server-base.sh нечем: занят ли apt, узнаём по его ошибке.
# На только что арендованном сервере он часто занят автообновлениями.
install_git() {
	local out deadline=$((SECONDS + 600)) told=0
	while ! out=$(export LC_ALL=C.UTF-8; { apt-get update -qq && apt-get install -y -qq git; } 2>&1); do
		case $out in
		*"Could not get lock"* | *"Unable to acquire the dpkg frontend lock"*) ;;
		*) die "не удалось поставить git: apt-get install git" ;;
		esac
		((SECONDS < deadline)) || die "система занята другой установкой уже 10 мин (часто это автообновления). Ничего не сломалось: подождите несколько минут и запустите команду ещё раз"
		((told)) || echo "  система занята другой установкой (часто это автообновления), ждём…"
		told=1
		sleep 5
	done
}

has_signers() { [[ -r $1 ]] && grep -Eqv '^[[:space:]]*(#|$)' "$1"; }

# signed КЛОН КОММИТ и latest_signed КЛОН АРГУМЕНТЫ_REV_LIST… — как в recipes/lib.sh: его ещё нельзя
# подключить, пока не проверена версия, из которой он взят.
signed() {
	git -C "$1" -c gpg.ssh.allowedSignersFile="$SIGNERS_CONF" -c gpg.program=false -c gpg.x509.program=false \
		verify-commit "$2" >/dev/null 2>&1
}

latest_signed() {
	local rev
	while read -r rev; do
		signed "$1" "$rev" && echo "$rev" && return 0
	done < <(git -C "$1" rev-list --first-parent "${@:2}" 2>/dev/null)
	return 1
}

# Ключи на сервере не трогаем: их могла сменить подписанная версия, вышедшая после этого скрипта.
install_signers() {
	has_signers "$SIGNERS_CONF" && return 0
	grep -Eqv '^[[:space:]]*(#|$)' < <(release_signers) || return 0
	mkdir -p "$(dirname "$SIGNERS_CONF")"
	release_signers >"$SIGNERS_CONF.tmp"
	chmod 644 "$SIGNERS_CONF.tmp"
	mv "$SIGNERS_CONF.tmp" "$SIGNERS_CONF"
}

# Первая установка: клон во временную папку, и в /opt/airlok он попадает только проверенным —
# иначе повторный запуск принял бы непроверенный клон за стоящую версию.
fresh_clone() {
	local tmp=$DIR.new rev
	rmdir "$DIR" 2>/dev/null || true
	[[ ! -e $DIR ]] || die "${DIR} уже есть, но это не git-клон: уберите его и запустите установку ещё раз"
	rm -rf "$tmp"
	if ! has_signers "$SIGNERS_CONF"; then
		git clone -q --depth 1 "$REPO" "$tmp" || { rm -rf "$tmp"; die "не удалось скачать рецепты из ${REPO}"; }
	else
		git clone -q --depth 50 --no-checkout "$REPO" "$tmp" || { rm -rf "$tmp"; die "не удалось скачать рецепты из ${REPO}"; }
		if ! rev=$(latest_signed "$tmp" HEAD); then
			rm -rf "$tmp"
			die "в ${REPO} нет версии, подписанной ключом Airlok, и ставить её нельзя. Напишите в поддержку: https://app.airlok.ru/support"
		fi
		git -C "$tmp" reset -q --hard "$rev"
	fi
	mv "$tmp" "$DIR"
}

# Повторный запуск обновляет так же, как update.sh: при ключах — только до подписанной версии.
update_clone() {
	local rev=FETCH_HEAD
	git -C "$DIR" fetch -q origin main || die "не удалось получить новую версию из ${REPO}"
	if has_signers "$SIGNERS_CONF"; then
		rev=$(latest_signed "$DIR" --ancestry-path HEAD..FETCH_HEAD) || rev=HEAD
		[[ $(git -C "$DIR" rev-parse "$rev") == $(git -C "$DIR" rev-parse FETCH_HEAD) ]] ||
			echo "  в main есть версия $(git -C "$DIR" rev-parse --short FETCH_HEAD) без подписи Airlok: она ещё не выпущена, ставим последнюю подписанную"
	fi
	git -C "$DIR" merge -q --ff-only "$rev" || die "не удалось обновить ${DIR}: есть локальные правки? git -C ${DIR} status"
}

[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ -r /etc/os-release ]] && [[ $(. /etc/os-release && echo "$ID") == ubuntu ]] || die "airlok ставится на Ubuntu"

dashboard=""
if [[ -n ${1:-} && ${1:-} != --* ]]; then
	dashboard=$1
	shift
fi
letsencrypt=0 le_email="" keys_only=0 strict=0 allow=()
grep -qx 'AIRLOK_MODE=strict' /etc/airlok/policy.env 2>/dev/null && strict=1
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
	--no-firewall) export AIRLOK_FIREWALL=off ;;
	--strict) strict=1 ;;
	--allow-repo)
		[[ -n ${2:-} ]] || die "после --allow-repo нужен репозиторий"
		allow+=(--allow-repo "$2")
		shift
		;;
	*) die "неизвестная опция: $1" ;;
	esac
	shift
done
((strict || !${#allow[@]})) || die "--allow-repo работает только вместе с --strict"
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
	[[ -n $token ]] || die "нет токена агента: вставьте токен из дашборда, когда установщик спросит"
fi

if ! command -v git >/dev/null; then
	echo "→ Ставим git"
	install_git
fi

install_signers
if [[ -d $DIR/.git ]] && ((strict)); then
	echo "→ Рецепты остаются на коммите $(git -C "$DIR" rev-parse --short HEAD): строгий режим обновляется только вручную"
elif [[ -d $DIR/.git ]]; then
	echo "→ Обновляем рецепты в ${DIR}"
	update_clone
else
	echo "→ Скачиваем рецепты в ${DIR}"
	fresh_clone
fi

"$DIR/recipes/server-base.sh" --skip-ssh

# До подключения к дашборду: агент не должен успеть выполнить ни одной лишней команды.
if ((strict)); then
	python3 "$DIR/recipes/strict.py" enable "${allow[@]}"
fi

if ((letsencrypt)); then
	"$DIR/recipes/server-base.sh" letsencrypt ${le_email:+"$le_email"}
fi

if [[ -n $dashboard ]]; then
	printf '%s\n' "$token" | "$DIR/recipes/notify.sh" dashboard "$dashboard"
fi

cat <<EOF

Сервер готов. Дальше:
  опубликовать проект:       sudo ${DIR}/recipes/app-deploy.sh /путь/к/проекту
  или из GitHub:         sudo ${DIR}/recipes/github-deploy.sh setup владелец/репозиторий
EOF
if [[ -n $dashboard ]]; then
	echo "  или кнопкой «Опубликовать» в дашборде: ${dashboard}"
fi
if ((strict)); then
	echo "  строгий режим: настройки — sudo ${DIR}/recipes/strict.py show, журнал команд — /var/log/airlok-commands.log"
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
	# Для CLI server-base.sh уже сказал, что вход подтвердит CLI.
	[[ ${AIRLOK_SSH_CONFIRM:-} == cli ]] || cat <<EOF

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
