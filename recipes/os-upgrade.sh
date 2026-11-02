#!/usr/bin/env bash
# Обновления системы по подтверждению человека: агент запускает отдельной службой, чтобы apt
# дошёл до конца, даже если он перезапустит самого агента.
#
#   os-upgrade.sh upgrade           — поставить обновления
#   os-upgrade.sh reboot ФАЙЛ       — запомнить работающие приложения и перезагрузить сервер
#   os-upgrade.sh check-apps ФАЙЛ   — после перезагрузки проверить, что они снова работают
#
# Как при apt upgrade: новые зависимости ставятся (так приходят новые ядра), ничего не удаляется,
# настройки, которые правили руками, остаются. Контейнеры обновление не трогает: в настройках
# Docker включён live-restore, поэтому они переживают и перезапуск самого Docker.

source "$(dirname "$0")/lib.sh"

LOCK_WAIT=${AIRLOK_APT_LOCK_WAIT:-600}
REBOOT_LOCK_WAIT=120
APPS_WAIT=180
MIN_FREE_MEM_MB=300
MIN_DISK_MB=1024
MIN_BOOT_MB=150
APT_LOG=/var/log/airlok-apt.log
# Сюда apt пишет проценты скачивания и установки, агент показывает их в дашборде.
APT_STATUS=${AIRLOK_OS_STATUS:-/dev/null}
DPKG_OPTS=(-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -o "DPkg::Lock::Timeout=${LOCK_WAIT}" -o Dpkg::Use-Pty=0)

running_apps() { docker ps --format '{{.Names}}' 2>/dev/null | sort || true; }

# stopped_apps СПИСОК — кто из списка сейчас не работает.
stopped_apps() { comm -23 <(sort <<<"$1") <(running_apps) | paste -sd ' ' -; }

# wait_apps СПИСОК — ждём, потому что Docker мог только что перезапуститься. Печатает остановившихся.
wait_apps() {
	local before=$1 deadline=$((SECONDS + APPS_WAIT)) gone
	[[ -n $before ]] || return 0
	while :; do
		gone=$(stopped_apps "$before")
		if [[ -z $gone ]] || ((SECONDS >= deadline)); then
			printf '%s' "$gone"
			return 0
		fi
		sleep 5
	done
}

preflight() {
	local mem disk boot
	mem=$(awk '/^(MemAvailable|SwapFree):/{n += $2} END {print int(n / 1024)}' /proc/meminfo)
	((mem >= MIN_FREE_MEM_MB)) || die "на сервере свободно всего ${mem} МБ памяти вместе со swap: приложениям и установке не хватит. Обновлять сейчас опасно"
	disk=$(df -Pm / | awk 'NR==2{print $4}')
	((disk >= MIN_DISK_MB)) || die "на диске свободно всего ${disk} МБ, а нужно хотя бы ${MIN_DISK_MB}: сначала освободите место"
	if mountpoint -q /boot; then
		boot=$(df -Pm /boot | awk 'NR==2{print $4}')
		((boot >= MIN_BOOT_MB)) || die "в разделе /boot свободно всего ${boot} МБ: новое ядро не поместится. Сначала удалите старые ядра: sudo apt autoremove"
	fi
	ok "памяти со swap свободно ${mem} МБ, на диске ${disk} МБ"
}

wait_apt() {
	python3 "$RECIPES_DIR/osupdates.py" wait-lock "$1" ||
		die "система занята другой установкой уже $(($1 / 60)) мин (часто это автообновления). Обновления не ставили: попробуйте ещё раз позже"
}

# apt_failure — причина простыми словами по журналу; подробности остаются в журнале команды.
apt_failure() {
	local tail
	tail=$(tail -40 "$APT_LOG")
	case $tail in
	*"No space left"* | *"not enough free space"* | *"You don't have enough free space"*) echo "на диске кончилось место: освободите его и запустите обновление ещё раз" ;;
	*"Could not get lock"* | *"Unable to acquire the dpkg frontend lock"*) echo "систему заняла другая установка: попробуйте ещё раз через несколько минут" ;;
	*"Temporary failure resolving"* | *"Failed to fetch"* | *"Could not connect"* | *"Connection timed out"*) echo "серверу не удалось скачать пакеты: зеркало Ubuntu недоступно или нет сети. Попробуйте ещё раз позже" ;;
	*"Cannot allocate memory"* | *"Out of memory"*) echo "установке не хватило памяти: попробуйте ещё раз, когда сервер будет меньше загружен" ;;
	*) echo "установка не прошла, система осталась рабочей, а что успело поставиться — поставилось. Подробности: ${APT_LOG} на сервере" ;;
	esac
}

# apt_step КОМАНДА… — вывод идёт и в журнал команды, и в файл, по которому ищем причину.
apt_step() {
	if ! "$@" 2>&1 | tee -a "$APT_LOG"; then
		die "$(apt_failure)"
	fi
}

upgrade() {
	need_root
	local before count left gone
	: >"$APT_LOG"
	step "Проверка"
	preflight
	before=$(running_apps)
	step "Обновления системы"
	wait_apt "$LOCK_WAIT"
	# Прошлая установка могла оборваться: пока её не довели, apt ничего не поставит.
	apt_step dpkg --force-confdef --force-confold --configure -a
	apt_step apt-get update -q -o Acquire::Languages=none -o Acquire::Retries=3 -o "DPkg::Lock::Timeout=${LOCK_WAIT}"
	count=$(python3 "$RECIPES_DIR/osupdates.py" count) || die "не удалось посчитать обновления: система пакетов не отвечает"
	if ((count == 0)); then
		echo "Готово: все обновления уже стоят"
		return
	fi
	ok "к установке: ${count}"
	apt_step apt-get -y -q --with-new-pkgs "${DPKG_OPTS[@]}" -o Acquire::Retries=3 -o APT::Status-Fd=3 upgrade 3>>"$APT_STATUS"
	# Скачанные пакеты больше не нужны, а диск на небольшом сервере мал.
	apt-get clean || true
	left=$(python3 "$RECIPES_DIR/osupdates.py" count) || left=0
	step "Приложения"
	gone=$(wait_apps "$before")
	[[ -z $gone ]] || die "обновления поставлены, но приложения остановились: ${gone}. Перезапустите их кнопкой в дашборде"
	ok "работают как прежде"
	if ((left > 0)); then
		echo "Готово: поставлено обновлений: $((count - left)), ещё ${left} не поставились: их можно поставить следующим запуском"
	else
		echo "Готово: поставлено обновлений: ${count}"
	fi
}

reboot_now() {
	need_root
	local apps=$1
	# Перезагрузка посреди чужой установки сломала бы пакеты.
	python3 "$RECIPES_DIR/osupdates.py" wait-lock "$REBOOT_LOCK_WAIT" ||
		die "система занята установкой: не перезагружаем посреди неё. Попробуйте ещё раз через несколько минут"
	running_apps >"$apps"
	sync
	echo "перезагружаем сервер"
	systemctl reboot
}

check_apps() {
	need_root
	local before gone
	before=$(cat "$1" 2>/dev/null || true)
	gone=$(wait_apps "$before")
	[[ -z $gone ]] || die "сервер перезагружен, но приложения не запустились: ${gone}. Перезапустите их кнопкой в дашборде"
	echo "Готово: сервер перезагружен, приложения работают"
}

case ${1:-} in
upgrade) upgrade ;;
reboot) reboot_now "${2:?нужен файл для списка приложений}" ;;
check-apps) check_apps "${2:?нужен файл со списком приложений}" ;;
*) die "неизвестная команда: ${1:-}" ;;
esac
