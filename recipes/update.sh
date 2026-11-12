#!/usr/bin/env bash
# Обновление airlok на сервере: агент и рецепты из GitHub, базовая настройка сервера и службы.
# Агент запускает его по команде из дашборда, которую подтвердил человек; вручную — то же самое:
#
#   sudo /opt/airlok/recipes/update.sh [--to КОММИТ]
#
# Ставится только коммит из ветки main, подписанный ключом из /etc/airlok/allowed_signers.

source "$(dirname "$0")/lib.sh"

need_root
dir=$(dirname "$RECIPES_DIR")
[[ -d $dir/.git ]] || die "${dir} — не git-клон, обновить его нельзя: поставьте airlok заново командой из дашборда"

step "Агент и рецепты"
before=$(agent_version)
# Ключи берём из уже стоящей версии, пока новая не получена: ей самой доверять ещё рано.
has_signers "$SIGNERS_CONF" || install_signers "$RECIPES_DIR/allowed_signers"
git -C "$dir" fetch -q origin main || die "не удалось получить новую версию из GitHub"
to=""
[[ ${1:-} == --to && ${2:-} =~ ^[0-9a-f]{7,40}$ ]] && to=$2
if strict_mode && [[ -z $to ]]; then
	# Новый код приходит, только когда человек посмотрел его и назвал коммит: в этом смысл режима.
	new=$(git -C "$dir" rev-parse --short FETCH_HEAD)
	if [[ $(git -C "$dir" rev-parse HEAD) == $(git -C "$dir" rev-parse FETCH_HEAD) ]]; then
		echo "Строгий режим: уже последняя версия ${before}"
		exit 0
	fi
	git -C "$dir" log --oneline HEAD..FETCH_HEAD | head -30
	git -C "$dir" diff --stat HEAD FETCH_HEAD | tail -15
	signature="ключи Airlok на сервере не заданы, подпись не проверяется"
	if has_signers "$SIGNERS_CONF"; then
		signature="подписана ключом Airlok"
		signed_commit "$dir" FETCH_HEAD || signature="НЕ подписана ключом Airlok — сервер её не поставит"
	fi
	cat <<EOF

Строгий режим: сервер обновляется только вручную, ничего не изменилось.
  версия ${new}: ${signature}
  посмотреть изменения:  git -C ${dir} diff HEAD ${new}
  поставить эту версию:  sudo ${RECIPES_DIR}/update.sh --to ${new}
EOF
	exit 0
fi
[[ -n $to || -z ${1:-} ]] || die "после --to нужен хеш коммита из ветки main"
if [[ -z $to ]] && has_signers "$SIGNERS_CONF"; then
	# Без названной версии ставим последнюю подписанную: неподписанная вершина main не должна
	# ронять обновление на каждом сервере, она просто ещё не выпущена.
	if ! to=$(latest_signed "$dir"); then
		to=FETCH_HEAD
		git -C "$dir" merge-base --is-ancestor HEAD FETCH_HEAD && to=HEAD
	fi
	if [[ $(git -C "$dir" rev-parse "$to") != $(git -C "$dir" rev-parse FETCH_HEAD) ]]; then
		echo "  в main есть версия $(git -C "$dir" rev-parse --short FETCH_HEAD) без подписи Airlok: она ещё не выпущена, сервер ставит последнюю подписанную"
	fi
fi
update_code "$dir" "${to:-FETCH_HEAD}"
# Список ключей новой версии принимаем: её подпись проверили прежние ключи (а если их не было, ей
# доверяем так же, как при установке). Так ключ меняют, не заходя на каждый сервер.
install_signers "$RECIPES_DIR/allowed_signers"
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
