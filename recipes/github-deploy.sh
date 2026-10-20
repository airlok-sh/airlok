#!/usr/bin/env bash
# Деплой из GitHub по deploy key с доступом только на чтение.
#
#   github-deploy.sh setup  владелец/репозиторий   — создать ключ и показать, куда его добавить
#   github-deploy.sh deploy владелец/репозиторий [ветка|коммит]
#                                                  — забрать код и выкатить; по умолчанию main
#   github-deploy.sh log    владелец/репозиторий [ветка]
#                                                  — последние коммиты, чтобы выбрать, что выкатить
#   github-deploy.sh webhook владелец/репозиторий [ветка]
#                                                  — выкатывать автоматически при push в ветку

source "$(dirname "$0")/lib.sh"

CONF_DIR=/etc/airlok/github
CACHE_DIR=/var/lib/airlok/git
HOOK_USER=airlok-hook
HOOK_SECRETS=/etc/airlok/webhook
QUEUE_DIR=/var/lib/airlok/queue

# Ключ хоста github.com, сверен с https://api.github.com/meta (SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU).
# Заранее известный ключ вместо «доверять при первом подключении»: подменить GitHub нельзя.
GITHUB_HOST_KEY="github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"

parse_repo() {
	local repo=${1:-}
	repo=${repo#https://github.com/}
	repo=${repo#git@github.com:}
	repo=${repo%.git}
	[[ $repo =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "укажите репозиторий в виде владелец/имя"
	REPO=$repo
	SLUG=${repo/\//__}
	KEY=$CONF_DIR/$SLUG/deploy_key
	GIT_DIR_PATH=$CACHE_DIR/$SLUG.git
}

git_ssh() {
	install -d -m 755 /etc/airlok
	write_if_changed /etc/airlok/github_known_hosts 0644 <<<"$GITHUB_HOST_KEY" || true
	export GIT_SSH_COMMAND="ssh -i ${KEY} -o IdentitiesOnly=yes -o UserKnownHostsFile=/etc/airlok/github_known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes"
}

setup() {
	need_root
	parse_repo "$1"
	install -d -m 700 "$CONF_DIR" "$CONF_DIR/$SLUG"
	if [[ ! -s $KEY ]]; then
		ssh-keygen -q -t ed25519 -N "" -C "airlok@$(hostname) ${REPO}" -f "$KEY"
		ok "создан ключ для ${REPO}"
	else
		ok "ключ для ${REPO} уже есть"
	fi
	cat <<EOF

Добавьте этот ключ в GitHub: ${REPO} → Settings → Deploy keys → Add deploy key.
Галочку «Allow write access» НЕ ставьте: серверу нужен только доступ на чтение.

$(cat "${KEY}.pub")

Затем выкатите: $0 deploy ${REPO}
EOF
}

fetch() {
	git_ssh
	[[ -s $KEY ]] || die "нет ключа для ${REPO}, сначала: $0 setup ${REPO}"
	install -d -m 700 "$CACHE_DIR"
	if [[ ! -d $GIT_DIR_PATH ]]; then
		git init -q --bare "$GIT_DIR_PATH"
		git --git-dir="$GIT_DIR_PATH" remote add origin "git@github.com:${REPO}.git"
	fi
	local err
	if ! err=$(git --git-dir="$GIT_DIR_PATH" fetch -q --prune origin '+refs/heads/*:refs/heads/*' 2>&1); then
		case $err in
		*"Permission denied"* | *"Repository not found"* | *"Could not read from remote"*)
			die "GitHub не пустил по ключу. Проверьте, что ключ добавлен в Deploy keys репозитория ${REPO}: $0 setup ${REPO}" ;;
		*) die "не удалось получить код из GitHub: ${err}" ;;
		esac
	fi
	ok "код получен из github.com/${REPO}"
}

# Ветку или коммит превращаем в полный хеш. Ссылку проверяем по формату заранее:
# значение, начинающееся с «-», git принял бы за опцию.
resolve() {
	local ref=$1 sha
	[[ $ref =~ ^[A-Za-z0-9._/][A-Za-z0-9._/-]*$ ]] || die "некорректное имя ветки или коммита: ${ref}"
	if sha=$(git --git-dir="$GIT_DIR_PATH" rev-parse -q --verify "refs/heads/${ref}^{commit}" 2>/dev/null); then
		REF_KIND="ветка ${ref}"
	elif [[ $ref =~ ^[0-9a-f]{7,40}$ ]] && sha=$(git --git-dir="$GIT_DIR_PATH" rev-parse -q --verify "${ref}^{commit}" 2>/dev/null); then
		REF_KIND="коммит"
	else
		die "в ${REPO} нет ветки или коммита ${ref}"
	fi
	SHA=$sha
}

deploy_trap() {
	local code=$1
	rm -rf "${SRC:-}"
	# После передачи в app-deploy.sh об итоге сообщает уже он.
	if ((code != 0 && ! HANDED_OFF)); then
		notify "❌ ${REPO}: выкатка не началась: ${DIE_MSG:-неизвестная ошибка}"
	fi
}

deploy() {
	need_root
	parse_repo "$1"
	local ref=${2:-main}
	HANDED_OFF=0
	SRC=""
	trap 'deploy_trap $?' EXIT
	fetch
	resolve "$ref"
	SRC=$(mktemp -d /tmp/airlok-src.XXXXXX)
	local src=$SRC
	# git archive, а не checkout: в релиз попадает только содержимое коммита, без .git и истории.
	git --git-dir="$GIT_DIR_PATH" archive "$SHA" | tar -x -C "$src"
	[[ -f $src/airlok.yml ]] || die "в корне ${REPO} нет airlok.yml"
	local subject
	subject=$(git --git-dir="$GIT_DIR_PATH" log -1 --format='%s' "$SHA")
	ok "${REF_KIND}: ${SHA:0:12} «${subject}»"
	HANDED_OFF=1
	AIRLOK_RELEASE_ID=${SHA:0:12} AIRLOK_RELEASE_TITLE=$subject "$RECIPES_DIR/app-deploy.sh" "$src"
}

log() {
	need_root
	parse_repo "$1"
	fetch
	resolve "${2:-main}"
	git --git-dir="$GIT_DIR_PATH" log -15 --format='%h  %ad  %s' --date=format:'%Y-%m-%d %H:%M' "$SHA"
}

install_webhook_services() {
	id "$HOOK_USER" >/dev/null 2>&1 ||
		useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$HOOK_USER"
	install -d -m 750 -o root -g "$HOOK_USER" "$HOOK_SECRETS"
	install -d -m 770 -o root -g "$HOOK_USER" "$QUEUE_DIR"

	local changed=no
	write_if_changed /etc/systemd/system/airlok-webhook.service 0644 <<EOF && changed=yes
[Unit]
Description=airlok: приём webhook GitHub
After=network.target

[Service]
User=${HOOK_USER}
Group=${HOOK_USER}
ExecStart=/usr/bin/python3 ${RECIPES_DIR}/webhook.py
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ReadWritePaths=${QUEUE_DIR}
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryMax=64M

[Install]
WantedBy=multi-user.target
EOF

	# Очередь разбирает root: деплою нужны его права, но от webhook сюда приходит
	# только «репозиторий + хеш», и оба значения проверяются ещё раз.
	write_if_changed /usr/local/sbin/airlok-deploy-queue 0755 <<EOF || true
#!/usr/bin/env bash
set -uo pipefail
shopt -s nullglob
exec 9>/run/airlok-deploy-queue.lock
flock 9
while :; do
	jobs=(${QUEUE_DIR}/*.job)
	((\${#jobs[@]})) || break
	for job in "\${jobs[@]}"; do
		read -r repo sha <"\$job" || true
		# Удаляем до выкатки: push во время деплоя создаст новую задачу, и цикл её подхватит.
		rm -f "\$job"
		if [[ ! \$repo =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\$ || ! \$sha =~ ^[0-9a-f]{40}\$ ]]; then
			echo "пропущена некорректная задача \$job"
			continue
		fi
		echo "выкатка \$repo \${sha:0:12}"
		if ${RECIPES_DIR}/github-deploy.sh deploy "\$repo" "\$sha" </dev/null; then
			echo "выкатка \$repo \${sha:0:12} завершена"
		else
			echo "выкатка \$repo \${sha:0:12} не удалась"
		fi
	done
done
EOF

	write_if_changed /etc/systemd/system/airlok-deploy-queue.service 0644 <<'EOF' && changed=yes
[Unit]
Description=airlok: выкатки из очереди webhook
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/airlok-deploy-queue
EOF
	write_if_changed /etc/systemd/system/airlok-deploy-queue.path 0644 <<EOF && changed=yes
[Unit]
Description=airlok: следить за очередью выкаток

[Path]
PathExistsGlob=${QUEUE_DIR}/*.job

[Install]
WantedBy=paths.target
EOF
	[[ $changed == yes ]] && systemctl daemon-reload
	systemctl enable --now airlok-webhook.service airlok-deploy-queue.path >/dev/null 2>&1
	[[ $changed == yes ]] && systemctl restart airlok-webhook.service

	if write_if_changed /etc/nginx/snippets/airlok-webhook.conf 0644 <<'EOF'; then
location ^~ /_airlok/hook/ {
    proxy_pass http://127.0.0.1:9417;
    client_max_body_size 1m;
}
EOF
		nginx -t -q || die "nginx не принял настройку webhook"
		systemctl reload nginx
	fi
	ok "приёмник работает от ${HOOK_USER}, выкатки выполняет очередь от root"
}

webhook() {
	need_root
	parse_repo "$1"
	local branch=${2:-main}
	[[ $branch =~ ^[A-Za-z0-9._/][A-Za-z0-9._/-]*$ ]] || die "некорректное имя ветки: ${branch}"
	[[ -s $KEY ]] || die "нет ключа для ${REPO}, сначала: $0 setup ${REPO}"
	install_webhook_services

	local secret=$HOOK_SECRETS/$SLUG.secret
	if [[ ! -s $secret ]]; then
		(umask 027 && openssl rand -hex 32 >"$secret")
	fi
	# Права выставляем каждый раз, а не только при создании: иначе секрет, созданный
	# раньше другим пользователем приёмника, ему будет не прочитать.
	chown "root:${HOOK_USER}" "$secret"
	chmod 640 "$secret"
	printf '%s\n' "$branch" | write_if_changed "$HOOK_SECRETS/$SLUG.branch" 0644 || true

	local domain
	domain=$(grep -h '^ *server_name' /etc/nginx/sites-enabled/airlok-*.conf 2>/dev/null | awk '{gsub(";", "", $2); print $2; exit}')
	if ! grep -qs 'airlok-webhook' /etc/nginx/sites-enabled/airlok-*.conf; then
		note "сайт ещё не знает про webhook: выкатите приложение один раз, чтобы обновился конфиг nginx"
	fi
	ok "push в ветку ${branch} будет выкатываться автоматически"
	cat <<EOF

Добавьте webhook в GitHub: ${REPO} → Settings → Webhooks → Add webhook.
  Payload URL:  https://${domain:-ваш-домен}/_airlok/hook/${SLUG}
  Content type: application/json
  Secret:       содержимое файла ${secret}
  События:      Just the push event
EOF
}

case ${1:-} in
setup) setup "${2:-}" ;;
deploy) deploy "${2:-}" "${3:-}" ;;
log) log "${2:-}" "${3:-}" ;;
webhook) webhook "${2:-}" "${3:-}" ;;
*) die "использование: $0 setup|deploy|log|webhook владелец/репозиторий [ветка|коммит]" ;;
esac
