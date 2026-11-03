#!/usr/bin/env bash
# Обновление airlok на сервере: агент и рецепты из GitHub, базовая настройка сервера и службы.
# Агент запускает его по команде из дашборда, которую подтвердил человек; вручную — то же самое:
#
#   sudo /opt/airlok/recipes/update.sh

source "$(dirname "$0")/lib.sh"

need_root
dir=$(dirname "$RECIPES_DIR")
[[ -d $dir/.git ]] || die "${dir} — не git-клон, обновить его нельзя: поставьте airlok заново командой из дашборда"

step "Агент и рецепты"
before=$(agent_version)
if strict_mode; then
	# Новый код приходит, только когда человек посмотрел его и назвал коммит: в этом смысл режима.
	git -C "$dir" fetch -q origin main || die "не удалось получить новую версию из GitHub"
	if [[ ${1:-} != --to || ! ${2:-} =~ ^[0-9a-f]{7,40}$ ]]; then
		new=$(git -C "$dir" rev-parse --short FETCH_HEAD)
		if [[ $(git -C "$dir" rev-parse HEAD) == $(git -C "$dir" rev-parse FETCH_HEAD) ]]; then
			echo "Строгий режим: уже последняя версия ${before}"
			exit 0
		fi
		git -C "$dir" log --oneline HEAD..FETCH_HEAD | head -30
		git -C "$dir" diff --stat HEAD FETCH_HEAD | tail -15
		cat <<EOF

Строгий режим: сервер обновляется только вручную, ничего не изменилось.
  посмотреть изменения:  git -C ${dir} diff HEAD ${new}
  поставить эту версию:  sudo ${RECIPES_DIR}/update.sh --to ${new}
EOF
		exit 0
	fi
	git -C "$dir" merge -q --ff-only "$2" || die "коммит ${2} не продолжает текущую версию ${dir}"
else
	git -C "$dir" pull -q --ff-only || die "не удалось обновить ${dir}: есть локальные правки? git -C ${dir} status"
fi
after=$(agent_version)
if [[ $before == "$after" ]]; then
	ok "уже последняя версия ${after}"
else
	ok "${before} → ${after}"
fi

# Дальше работают уже новые скрипты: они досоздают то, что появилось в этой версии. Причину
# неудачи они пишут сами.
"$RECIPES_DIR/server-base.sh" --skip-ssh || exit 1
if [[ -r $DASHBOARD_CONF ]]; then
	"$RECIPES_DIR/notify.sh" install-healthcheck || exit 1
	AIRLOK_DEFER_AGENT_RESTART=1 "$RECIPES_DIR/notify.sh" install-agent || exit 1
	"$RECIPES_DIR/notify.sh" install-logs || exit 1
	# Проверка сайтов заодно отправляет heartbeat: новая версия появится в дашборде сразу.
	systemctl start --no-block airlok-healthcheck.service
fi
if [[ $before == "$after" ]]; then
	echo "Готово: обновлять было нечего, версия ${after}"
else
	echo "Готово: агент обновлён с ${before} до ${after}"
fi
