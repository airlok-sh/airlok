#!/usr/bin/env bash
# Данные приложения при «Переехать» в дашборде: база, загрузки и постоянная папка уходят в
# хранилище по временной ссылке и приходят оттуда на новом месте.
#
#   app-data.sh export ИМЯ  — остановить приложение и выгрузить его данные
#   app-data.sh import ИМЯ  — заменить данные выгруженными и запустить приложение
#   app-data.sh resume ИМЯ  — запустить остановленное переездом снова: перенос не удался
#   app-data.sh backup ИМЯ  — копия базы и постоянной папки в хранилище, приложение работает
#   app-data.sh restore ИМЯ — заменить их копией из хранилища (на время замены приложение стоит)
#
# Ссылки — в AIRLOK_DB_URL, AIRLOK_FILES_URL и AIRLOK_DATA_URL (PUT при выгрузке, GET при загрузке),
# а не в аргументах: там их было бы видно в ps. Пустая ссылка — эта часть не нужна. Приложение
# останавливается целиком, кроме базы, и задачи по расписанию тоже: иначе записи, сделанные
# во время переноса, остались бы только на старом месте.

source "$(dirname "$0")/lib.sh"

WORK=/var/lib/airlok/move
# Больше этого постоянная папка в хранилище не копируется: 11 копий такого размера на каждое приложение
# бесплатного сервера. Копии на самом сервере это не ограничивает.
DATA_BACKUP_MAX_BYTES=$((2 * 1024 * 1024 * 1024))
# 1, пока приложение остановлено нами: при любом выходе с ошибкой оно запустится снова.
RESTART_ON_EXIT=0
UNPAUSE_ON_EXIT=0

compose() { docker compose -f "$APP_DIR/compose.yml" --project-directory "$APP_DIR" "$@" </dev/null; }
# psql_in — psql в контейнере базы со входом из конвейера: compose() вход закрывает.
psql_in() { docker compose -f "$APP_DIR/compose.yml" --project-directory "$APP_DIR" exec -T db psql -U app -d app -q -v ON_ERROR_STOP=1; }
has_db() { grep -q '^  db:' "$APP_DIR/compose.yml"; }
has_data() { grep -q '/shared/data:/data' "$APP_DIR/compose.yml"; }

# pack_dir КАТАЛОГ АРХИВ — только файлы и каталоги: ссылку, которую оставило приложение, при
# распаковке на новом месте можно было бы направить за пределы папки.
pack_dir() {
	(cd "$1" && find . \( -type f -o -type d \) -print0 | tar --null --no-recursion -czf "$2" -T -)
}

# unpack_dir АРХИВ КАТАЛОГ — от пользователя приложения: архив не запишет ничего за пределами папки.
# Читает его root (папка переноса закрыта), пользователю приложения достаётся только поток.
unpack_dir() {
	find "$2" -mindepth 1 -delete
	runuser -u "app-$NAME" -- tar -xzf - -C "$2" --no-same-owner --no-same-permissions <"$1"
}

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
		pack_dir "$uploads" "$WORK/files.tar.gz"
		put "$WORK/files.tar.gz" "$AIRLOK_FILES_URL"
		ok "загрузки: $(du -h "$WORK/files.tar.gz" | cut -f1) в сжатом виде"
	fi
	if [[ -n ${AIRLOK_DATA_URL:-} ]] && has_data; then
		step "Выгрузка постоянной папки"
		pack_dir "$APP_DIR/shared/data" "$WORK/data.tar.gz"
		put "$WORK/data.tar.gz" "$AIRLOK_DATA_URL"
		ok "постоянная папка: $(du -h "$WORK/data.tar.gz" | cut -f1) в сжатом виде"
	fi
	RESTART_ON_EXIT=0
	printf 'Готово: данные выгружены, приложение остановлено — дальше оно работает на новом месте\n'
}

# wipe_sql очищает базу перед загрузкой: дамп создаёт заново всё. Схемы — все, а не только public:
# например, Drizzle держит миграции в схеме drizzle. Большие объекты не лежат ни в какой схеме.
wipe_sql() {
	cat <<'SQL'
DO $$
DECLARE s name;
BEGIN
	FOR s IN SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' LOOP
		EXECUTE format('DROP SCHEMA %I CASCADE', s);
	END LOOP;
	PERFORM lo_unlink(oid) FROM pg_largeobject_metadata;
END $$;
CREATE SCHEMA public;
SQL
}

# load_dump ФАЙЛ — заменить базу дампом. Одна транзакция: если дамп не загрузится или оборвётся, база
# останется прежней. Без COMMIT psql на конце входа откатит её сам.
load_dump() {
	{
		printf 'SET client_min_messages = warning;\nBEGIN;\n'
		wipe_sql
		# Дамп с хостинга сделан pg_dump 17: настройки transaction_timeout 16-й Postgres не знает, а
		# \restrict — psql из старого образа postgres:16, который мог остаться на сервере. Свой COMMIT дампа
		# после больших объектов зафиксировал бы загрузку до индексов и ограничений.
		gunzip -c "$1" | grep -v -e '^SET transaction_timeout' -e '^\\restrict ' -e '^\\unrestrict ' |
			sed '/^SELECT pg_catalog\.lo_close(/{n;n;/^COMMIT;$/d}' &&
			printf 'COMMIT;\n'
	} | psql_in >/dev/null
}

import_data() {
	local uploads=$APP_DIR/shared/uploads
	if [[ -n ${AIRLOK_DATA_URL:-} ]] && ! has_data; then
		die "у приложения на этом сервере нет постоянной папки: включите её в airlok.yml (capabilities.data) и опубликуйте заново"
	fi
	if [[ -n ${AIRLOK_DB_URL:-} ]] && ! has_db; then
		die "у приложения на этом сервере нет базы: включите её в airlok.yml (database) и опубликуйте заново"
	fi
	if [[ -n ${AIRLOK_FILES_URL:-} && ! -d $uploads ]]; then
		die "у приложения на этом сервере нет загрузок: включите их в airlok.yml (uploads) и опубликуйте заново"
	fi
	stop_app
	if [[ -n ${AIRLOK_DB_URL:-} ]]; then
		step "Загрузка базы"
		get "$AIRLOK_DB_URL" "$WORK/db.sql.gz"
		load_dump "$WORK/db.sql.gz" || die "дамп базы не загрузился, база осталась прежней"
		ok "база заменена данными из хранилища"
	fi
	if [[ -n ${AIRLOK_FILES_URL:-} ]]; then
		step "Загрузка файлов"
		get "$AIRLOK_FILES_URL" "$WORK/files.tar.gz"
		unpack_dir "$WORK/files.tar.gz" "$uploads" || die "архив загрузок не распаковался"
		ok "загрузки заменены файлами со старого места"
	fi
	if [[ -n ${AIRLOK_DATA_URL:-} ]]; then
		step "Загрузка постоянной папки"
		get "$AIRLOK_DATA_URL" "$WORK/data.tar.gz"
		unpack_dir "$WORK/data.tar.gz" "$APP_DIR/shared/data" || die "архив постоянной папки не распаковался"
		ok "постоянная папка заменена данными из хранилища"
	fi
	start_app
	printf 'Готово: %s\n' "${1:-данные перенесены, приложение запущено}"
}

# backup_data — копия базы и постоянной папки в хранилище. Копировать нечего — дашборд увидит, что
# файлов в хранилище не появилось.
backup_data() {
	[[ -n ${AIRLOK_DB_URL:-} ]] || die "нет ссылки на хранилище для копии"
	if has_db; then
		step "Копия базы"
		compose exec -T db pg_dump -U app --no-owner --no-privileges app | gzip >"$WORK/db.sql.gz"
		put "$WORK/db.sql.gz" "$AIRLOK_DB_URL"
		ok "база: $(du -h "$WORK/db.sql.gz" | cut -f1) в сжатом виде"
	fi
	if [[ -n ${AIRLOK_DATA_URL:-} ]] && has_data; then
		step "Копия постоянной папки"
		# На время архива приложение замирает: иначе файл SQLite попал бы в копию посреди записи.
		UNPAUSE_ON_EXIT=1
		compose pause app >/dev/null 2>&1 || true
		pack_dir "$APP_DIR/shared/data" "$WORK/data.tar.gz"
		compose unpause app >/dev/null 2>&1 || true
		UNPAUSE_ON_EXIT=0
		if (($(stat -c %s "$WORK/data.tar.gz") > DATA_BACKUP_MAX_BYTES)); then
			has_db && note "постоянная папка больше 2 ГБ в сжатом виде: в хранилище ушла только база"
			has_db || die "постоянная папка больше 2 ГБ в сжатом виде: в хранилище она не копируется, копии остались на сервере в ${APP_DIR}/shared/backups"
		else
			put "$WORK/data.tar.gz" "$AIRLOK_DATA_URL"
			ok "постоянная папка: $(du -h "$WORK/data.tar.gz" | cut -f1) в сжатом виде"
		fi
	fi
	printf 'Готово: копия в хранилище\n'
}

# on_exit убирает временные файлы и, если работа оборвалась после остановки, запускает
# приложение снова: старое место не должно остаться без сайта из-за сбоя хранилища.
on_exit() {
	local rc=$?
	rm -f "$WORK/db.sql.gz" "$WORK/files.tar.gz" "$WORK/data.tar.gz"
	if ((UNPAUSE_ON_EXIT)); then
		compose unpause app >/dev/null 2>&1 || true
	fi
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
	[[ -f $APP_DIR/compose.yml ]] || die "приложение ${NAME} на этом сервере ещё не публиковали"
	exec 8>"/run/airlok-deploy-${NAME}.lock"
	flock -w 60 8 || die "у ${NAME} идёт публикация: перенос данных — после неё"
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
		[[ -n ${AIRLOK_DB_URL:-}${AIRLOK_DATA_URL:-} ]] || die "нет ссылки на копию"
		AIRLOK_FILES_URL="" import_data "данные восстановлены из копии, приложение запущено"
		;;
	*) die "app-data.sh export|import|resume|backup|restore ИМЯ" ;;
	esac
}

main "$@"
