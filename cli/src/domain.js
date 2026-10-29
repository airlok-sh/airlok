// Команда domain: свой домен приложения на своём сервере. Домен главнее app.domain в airlok.yml,
// уходит агенту, когда A-запись указывает на сервер, и включается следующей выкаткой.

import { ApiError } from './api.js'
import { findApp } from './env.js'

export class DomainError extends Error {}

// domainState — none: своего домена нет; waiting_dns: ждём A-запись на сервер; waiting_deploy:
// DNS готов, домен включится следующей выкаткой; live: сайт уже на нём (так сообщил агент).
export function domainState(app, d) {
  if (!d) return 'none'
  if (!d.verified) return 'waiting_dns'
  return app.domain === d.hostname ? 'live' : 'waiting_deploy'
}

export async function domainCommand(api, sub, args, opts, io) {
  if (!['show', 'add', 'check', 'rm'].includes(sub)) throw new DomainError('domain show | add ДОМЕН | check | rm — см. npx @airlok/cli help')
  if (!opts.app) throw new DomainError('Укажите приложение: --app <имя> или --app <сервер>/<имя>')
  const app = findApp(await api.allApps(), opts.app)
  if (app.hosted) throw new DomainError(`${app.name} на хостинге: свои домены там подключаются в дашборде или через ИИ-агента (add_domain).`)
  let d
  switch (sub) {
    case 'show':
      d = (await api.serverDomains()).find((x) => x.app_id === app.id)
      break
    case 'add':
      if (args.length !== 1) throw new DomainError(`Укажите один домен: npx @airlok/cli domain add shop.example.ru --app ${opts.app}`)
      d = await api.setServerDomain(app.id, args[0]).catch(explain)
      break
    case 'check':
      d = await api.checkServerDomain(app.id).catch(explain)
      break
    case 'rm':
      await api.removeServerDomain(app.id).catch(explain)
      if (opts.json) io.out(JSON.stringify({ status: 'ok', app: app.name, state: 'none' }))
      io.err('✓ Домен отключён: со следующей выкатки сайт вернётся на адрес из airlok.yml или на адрес по умолчанию.')
      return 0
  }
  const state = domainState(app, d)
  const ip = d ? (await api.servers()).find((s) => s.id === app.server_id)?.public_ip ?? '' : ''
  const record = d && ip ? { type: 'A', name: d.display, value: ip } : null
  if (opts.json) io.out(JSON.stringify({ status: 'ok', app: app.name, address: app.domain, domain: d ?? null, state, record }))
  for (const line of report(app, opts.app, d, state, ip)) io.err(line)
  return 0
}

function report(app, ref, d, state, ip) {
  const address = app.domain ? `Сейчас сайт открывается на ${app.domain}.` : 'Адрес появится после первой выкатки.'
  switch (state) {
    case 'none':
      return [`У ${app.name} нет своего домена. ${address}`, `Подключить: npx @airlok/cli domain add shop.example.ru --app ${ref}`]
    case 'waiting_dns':
      if (!ip) return [`${d.display}: ждём DNS. IP сервера ${app.server_name} пока неизвестен — он появится, когда агент выйдет на связь.`]
      return [
        `${d.display}: ждём DNS${d.error ? ` — ${d.error}` : ''}.`,
        'Заведите у регистратора A-запись и удалите другие A-записи этого имени:',
        `  A  ${d.display}  →  ${ip}`,
        `airlok проверяет её сам каждую минуту, а пока она не готова, выкатки идут на прежний адрес. Проверить сейчас: npx @airlok/cli domain check --app ${ref}`,
      ]
    case 'waiting_deploy':
      return [
        `✓ ${d.display} указывает на сервер. Домен включится следующей выкаткой: push в ветку, кнопка в дашборде или deploy у ИИ-агента.`,
        'Если выкатка прошла, а домен не включился, на сервере старый агент: обновите его в дашборде (Серверы → «Обновить»).',
      ]
    default:
      return [`✓ Сайт работает на ${d.display}.`]
  }
}

function explain(err) {
  if (!(err instanceof ApiError)) throw err
  switch (err.code) {
    case 'invalid_domain':
      throw new DomainError('Домен не подходит: нужно имя вроде shop.example.ru или магазин.рф, не просто зона вроде .ru.')
    case 'platform_domain':
      throw new DomainError('Адреса на домене хостинга airlok выдаёт сам — укажите свой домен.')
    case 'domain_taken':
      throw new DomainError('Этот домен уже у другого приложения на этом сервере.')
    case 'preview_settings':
      throw new DomainError('Это превью ветки: свой домен подключается у основного приложения.')
    case 'no_domain':
      throw new DomainError('У приложения нет своего домена: npx @airlok/cli domain add ДОМЕН --app <имя>')
    default:
      throw err
  }
}
