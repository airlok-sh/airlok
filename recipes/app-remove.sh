#!/usr/bin/env bash
# Убирает приложение с сервера: останавливает его контейнеры, снимает сайт nginx, выключает
# задачи по расписанию и бэкапы, удаляет код. Данные остаются: том базы ИМЯ_pgdata и
# /srv/apps/ИМЯ/shared с бэкапами, загрузками и секретами — выкатка под тем же именем их подхватит.
#
#   app-remove.sh ИМЯ [--with-data]
#
# --with-data удаляет и данные, без возврата. Сбор логов останавливается сам: контейнеров
# больше нет, а дашборд перестаёт называть приложение. Приложения на сервере нет — не ошибка:
# агент повторяет команду, если не дождался ответа дашборда.

source "$(dirname "$0")/lib.sh"

DOMAIN_RE='^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63}|xn--[a-z0-9-]{1,59})$'

compose() { docker compose -f "$APP_DIR/compose.yml" --project-directory "$APP_DIR" "$@" </dev/null; }

# stop_jobs — таймеры задач по расписанию и бэкапа: без приложения они только падали бы.
stop_jobs() {
	rm -f "$APP_DIR/shared/cron.json"
	python3 "$RECIPES_DIR/cron.py" sync "$NAME" >/dev/null || die "задачи по расписанию не выключились — причина выше"
	# Таймер выключен, но задача, запущенная им раньше, ещё может идти.
	systemctl stop "airlok-cron@${NAME}_*.service" >/dev/null 2>&1 || true
	systemctl disable --now "airlok-backup@${NAME}.timer" >/dev/null 2>&1 || true
	ok "задачи по расписанию и бэкапы выключены"
}

# last_backup — свежий дамп перед остановкой: забрать данные из файла проще, чем из тома.
last_backup() {
	[[ -f $APP_DIR/compose.yml && -n $(compose ps -q --status running db 2>/dev/null) ]] || return 0
	if /usr/local/sbin/airlok-backup "$NAME"; then
		ok "свежий дамп базы в ${APP_DIR}/shared/backups"
	else
		note "дамп базы не снялся, том ${NAME}_pgdata остаётся как есть"
	fi
}

stop_containers() {
	local down=(down --remove-orphans) ids=()
	((WITH_DATA)) && down+=(--volumes)
	if [[ -f $APP_DIR/compose.yml ]]; then
		compose "${down[@]}" || die "контейнеры приложения не остановились — причина выше"
	fi
	# Разовые контейнеры задач и остатки прошлой попытки compose.yml уже не знает.
	mapfile -t ids < <(docker ps -aq --filter "label=com.docker.compose.project=${NAME}")
	((${#ids[@]} == 0)) || docker rm -f "${ids[@]}" >/dev/null || die "не удалось удалить контейнеры приложения"
	docker network rm "${NAME}_default" >/dev/null 2>&1 || true
	if ((WITH_DATA)); then
		mapfile -t ids < <(docker volume ls -q --filter "label=com.docker.compose.project=${NAME}")
		((${#ids[@]} == 0)) || docker volume rm "${ids[@]}" >/dev/null || die "не удалось удалить том базы"
	fi
	ok "контейнеры остановлены и удалены"
}

remove_site() {
	local enabled=/etc/nginx/sites-enabled/airlok-${NAME}.conf available=/etc/nginx/sites-available/airlok-${NAME}.conf conf domain=""
	for conf in "$enabled" "$available"; do
		[[ -f $conf ]] && domain=$(awk '$1=="server_name"{gsub(";", "", $2); print $2; exit}' "$conf") && break
	done
	if [[ -e $enabled || -L $enabled || -e $available ]]; then
		rm -f "$enabled" "$available"
		nginx -t -q || die "nginx не принял конфиг без сайта ${NAME}: сайт снят с диска, но nginx его ещё отдаёт — исправьте ошибку из nginx -t и перезагрузите nginx"
		systemctl reload nginx
		ok "сайт ${domain:-$NAME} снят"
	fi
	rm -f "/etc/ssl/airlok/${NAME}.crt" "/etc/ssl/airlok/${NAME}.key"
	# Сертификат одного домена: домен двух сайтов сразу app-deploy.sh не допускает.
	if [[ $domain =~ $DOMAIN_RE && -e /etc/letsencrypt/renewal/${domain}.conf ]] && command -v certbot >/dev/null; then
		if certbot delete --cert-name "$domain" --non-interactive >/dev/null 2>&1; then
			ok "сертификат Let's Encrypt для ${domain} удалён"
		else
			note "сертификат Let's Encrypt для ${domain} не удалился: certbot будет пытаться его продлить"
		fi
	fi
}

remove_files() {
	if ! ((WITH_DATA)); then
		rm -rf --one-file-system "$APP_DIR/compose.yml" "$APP_DIR/current" "$APP_DIR/releases"
		return
	fi
	local unit
	unit=$(systemd-escape --path --suffix=mount "$APP_DIR/shared/uploads")
	if [[ -e /etc/systemd/system/$unit ]]; then
		systemctl disable --now "$unit" >/dev/null 2>&1 || true
		rm -f "/etc/systemd/system/$unit"
		systemctl daemon-reload
	fi
	findmnt -n "$APP_DIR/shared/uploads" >/dev/null && die "загрузки ${APP_DIR}/shared/uploads не отмонтировались"
	rm -rf --one-file-system "$APP_DIR"
	# Пользователя оставляем, пока целы загрузки: его uid система отдала бы следующему
	# приложению вместе с чужими файлами.
	if id "app-${NAME}" >/dev/null 2>&1; then
		userdel "app-${NAME}" || note "пользователь app-${NAME} не удалился"
	fi
	ok "код, база, бэкапы, загрузки и секреты удалены"
}

main() {
	need_root
	NAME=${1:-}
	WITH_DATA=0
	[[ $NAME =~ ^[a-z][a-z0-9-]{1,30}$ ]] || die "некорректное имя приложения: ${NAME}"
	case ${2:-} in
	"") ;;
	--with-data) WITH_DATA=1 ;;
	*) die "использование: $0 ИМЯ [--with-data]" ;;
	esac
	APP_DIR=$APPS_ROOT/$NAME

	exec 8>"/run/airlok-deploy-${NAME}.lock"
	flock -w 60 8 || die "у ${NAME} идёт выкатка: уберите приложение после неё"

	local kept=""
	step "Убираем ${NAME} с сервера"
	stop_jobs
	((WITH_DATA)) || last_backup
	stop_containers
	remove_site
	remove_files
	rm -f "/var/lib/airlok/health/${NAME}"

	if ((WITH_DATA)); then
		printf '\n%sГотово:%s приложение %s убрано с сервера вместе с данными\n' "$C_B" "$C_0" "$NAME"
		return
	fi
	docker volume inspect "${NAME}_pgdata" >/dev/null 2>&1 && kept="том базы ${NAME}_pgdata и "
	if [[ -d $APP_DIR/shared ]]; then
		kept+="${APP_DIR}/shared"
	else
		kept=${kept% и }
	fi
	printf '\n%sГотово:%s приложение %s убрано с сервера%s\n' "$C_B" "$C_0" "$NAME" "${kept:+, данные остались: ${kept}}"
}

main "$@"
