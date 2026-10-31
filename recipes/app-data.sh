#!/usr/bin/env bash
# Данные приложения при «Переехать» в дашборде: база и загрузки уходят в хранилище по временной
# ссылке и приходят оттуда на новом месте.
#
#   app-data.sh export ИМЯ  — остановить приложение и выгрузить базу и загрузки
#   app-data.sh import ИМЯ  — заменить базу и загрузки выгруженными и запустить приложение
#   app-data.sh resume ИМЯ  — запустить остановленное переездом снова: перенос не удался
#   app-data.sh backup ИМЯ  — копия базы в хранилище, приложение продолжает работать
#   app-data.sh restore ИМЯ — заменить базу копией из хранилища (на время замены приложение стоит)
#
# Ссылки — в AIRLOK_DB_URL и AIRLOK_FILES_URL (PUT при выгрузке, GET при загрузке), а не в
# аргументах: там их было бы видно в ps. Пустая ссылка — эта часть не нужна. Приложение
# останавливается целиком, кроме базы, и задачи по расписанию тоже: иначе записи, сделанные
# во время переноса, остались бы только на старом месте.

source "$(dirname "$0")/lib.sh"

WORK=/var/lib/airlok/move
# 1, пока приложение остановлено нами: при любом выходе с ошибкой оно запустится снова.
RESTART_ON_EXIT=0

compose() { docker compose -f "$APP_DIR/compose.yml" --project-directory "$APP_DIR" "$@" </dev/null; }
# psql_in — psql в контейнере базы со входом из конвейера: compose() вход закрывает.
psql_in() { docker compose -f "$APP_DIR/compose.yml" --project-directory "$APP_DIR" exec -T db psql -U app -d app -q -v ON_ERROR_STOP=1; }
has_db() { grep -q '^  db:' "$APP_DIR/compose.yml"; }

stop_app() {
	local services
	services=$(compose config --services | grep -vx db) || true
	# shellcheck disable=SC2086 # имена служб compose без пробелов
	[[ -n $services ]] && compose stop $services
	systemctl stop "airlok-cron@${NAME}_*.timer" 2>/dev/null || true
	RESTART_ON_EXIT=1
	ok "приложение остановлено, задачи по расписанию тоже"
}

start_app() {
	RESTART_ON_EXIT=0
	compose up -d || die "приложение не запустилось: docker compose -f ${APP_DIR}/compose.yml logs app"
	start_cron_timers "$NAME"
	ok "приложение запущено"
}

# put ФАЙЛ ССЫЛКА — выгрузить по временной ссылке хранилища.
put() {
	curl -sS --fail --retry 3 --max-time 3600 -T "$1" "$2" -o /dev/null || die "не удалось выгрузить $(basename "$1") в хранилище"
}

get() {
	curl -sS --fail --retry 3 --max-time 3600 -o "$2" "$1" || die "не удалось скачать $(basename "$2") из хранилища"
}

export_data() {
	stop_app
	if [[ -n ${AIRLOK_DB_URL:-} ]] && has_db; then
		step "Выгрузка базы"
		compose exec -T db pg_dump -U app --no-owner --no-privileges app | gzip >"$WORK/db.sql.gz"
		put "$WORK/db.sql.gz" "$AIRLOK_DB_URL"
		ok "база: $(du -h "$WORK/db.sql.gz" | cut -f1) в сжатом виде"
	fi
	local uploads=$APP_DIR/shared/uploads
	if [[ -n ${AIRLOK_FILES_URL:-} && -d $uploads ]]; then
		step "Выгрузка загрузок"
		# Только файлы и каталоги: ссылку, которую оставило приложение, при распаковке на новом
		# месте можно было бы направить за пределы папки загрузок.
		(cd "$uploads" && find . \( -type f -o -type d \) -print0 | tar --null --no-recursion -czf "$WORK/files.tar.gz" -T -)
		put "$WORK/files.tar.gz" "$AIRLOK_FILES_URL"
		ok "загрузки: $(du -h "$WORK/files.tar.gz" | cut -f1) в сжатом виде"
	fi
	RESTART_ON_EXIT=0
	printf 'Готово: данные выгружены, приложение остановлено — дальше оно работает на новом месте\n'
}

import_data() {
	local uploads=$APP_DIR/shared/uploads user=app-$NAME
	if [[ -n ${AIRLOK_DB_URL:-} ]] && ! has_db; then
		die "у приложения на этом сервере нет базы: включите её в airlok.yml (database) и выкатите заново"
	fi
	if [[ -n ${AIRLOK_FILES_URL:-} && ! -d $uploads ]]; then
		die "у приложения на этом сервере нет загрузок: включите их в airlok.yml (uploads) и выкатите заново"
	fi
	stop_app
	if [[ -n ${AIRLOK_DB_URL:-} ]]; then
		step "Загрузка базы"
		get "$AIRLOK_DB_URL" "$WORK/db.sql.gz"
		compose exec -T db psql -U app -d app -q -v ON_ERROR_STOP=1 -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;' >/dev/null
		# Дамп с хостинга сделан pg_dump 17: настройки transaction_timeout 16-й Postgres не знает, а
		# \restrict — psql из старого образа postgres:16, который мог остаться на сервере.
		gunzip -c "$WORK/db.sql.gz" | grep -v -e '^SET transaction_timeout' -e '^\\restrict ' -e '^\\unrestrict ' |
			psql_in >/dev/null || die "дамп базы не загрузился"
		ok "база заменена данными из хранилища"
	fi
	if [[ -n ${AIRLOK_FILES_URL:-} ]]; then
		step "Загрузка файлов"
		get "$AIRLOK_FILES_URL" "$WORK/files.tar.gz"
		find "$uploads" -mindepth 1 -delete
		# От пользователя приложения: архив не запишет ничего за пределами его папки загрузок.
		# Читает его root (папка переноса закрыта), пользователю приложения достаётся только поток.
		runuser -u "$user" -- tar -xzf - -C "$uploads" --no-same-owner --no-same-permissions <"$WORK/files.tar.gz" ||
			die "архив загрузок не распаковался"
		ok "загрузки заменены файлами со старого места"
	fi
	start_app
	printf 'Готово: %s\n' "${1:-данные перенесены, приложение запущено}"
}

# backup_data — копия базы в хранилище. Базы у приложения нет — нет и копии: дашборд увидит,
# что файл в хранилище не появился.
backup_data() {
	[[ -n ${AIRLOK_DB_URL:-} ]] || die "нет ссылки на хранилище для копии"
	if ! has_db; then
		printf 'Готово: у приложения нет базы — копировать нечего\n'
		return
	fi
	step "Копия базы"
	compose exec -T db pg_dump -U app --no-owner --no-privileges app | gzip >"$WORK/db.sql.gz"
	put "$WORK/db.sql.gz" "$AIRLOK_DB_URL"
	printf 'Готово: копия базы в хранилище, %s в сжатом виде\n' "$(du -h "$WORK/db.sql.gz" | cut -f1)"
}

# on_exit убирает временные файлы и, если работа оборвалась после остановки, запускает
# приложение снова: старое место не должно остаться без сайта из-за сбоя хранилища.
on_exit() {
	local rc=$?
	rm -f "$WORK/db.sql.gz" "$WORK/files.tar.gz"
	if ((RESTART_ON_EXIT)); then
		echo "не получилось — запускаем приложение снова"
		start_app
	fi
	exit "$rc"
}

main() {
	need_root
	local op=${1:-}
	NAME=${2:-}
	[[ $NAME =~ ^[a-z][a-z0-9-]{1,30}$ ]] || die "некорректное имя приложения: ${NAME}"
	APP_DIR=$APPS_ROOT/$NAME
	[[ -f $APP_DIR/compose.yml ]] || die "приложение ${NAME} на этом сервере ещё не выкатывали"
	exec 8>"/run/airlok-deploy-${NAME}.lock"
	flock -w 60 8 || die "у ${NAME} идёт выкатка: перенос данных — после неё"
	install -d -m 700 "$WORK"
	trap on_exit EXIT
	case $op in
	export) export_data ;;
	import) import_data ;;
	resume)
		start_app
		printf 'Готово: приложение снова запущено\n'
		;;
	backup) backup_data ;;
	restore)
		[[ -n ${AIRLOK_DB_URL:-} ]] || die "нет ссылки на копию базы"
		has_db || die "у приложения на этом сервере нет базы"
		AIRLOK_FILES_URL="" import_data "база восстановлена из копии, приложение запущено"
		;;
	*) die "app-data.sh export|import|resume|backup|restore ИМЯ" ;;
	esac
}

main "$@"
