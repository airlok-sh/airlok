// Замер скорости у посетителя (internal/speed): показатели Web Vitals этой загрузки страницы
// уходят на /_airlok/v, когда посетитель уходит со страницы или сворачивает её. Без cookies и
// без данных о посетителе — только путь страницы и числа.
(function () {
  var P = window.PerformanceObserver, perf = window.performance;
  if (!P || !perf || !perf.getEntriesByType) return;
  var v = {}, sent = false;
  // Пока вкладка в фоне, страница рисуется не для посетителя: такие отрисовки не считаем.
  var hiddenAt = document.visibilityState === 'hidden' ? 0 : Infinity;
  addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden' && hiddenAt === Infinity) hiddenAt = perf.now();
  }, true);
  function observe(type, cb, extra) {
    try {
      var init = { type: type, buffered: true };
      for (var k in extra) init[k] = extra[k];
      new P(function (list) { list.getEntries().forEach(cb); }).observe(init);
      return true;
    } catch (e) {
      return false;
    }
  }
  var nav = perf.getEntriesByType('navigation')[0];
  // Предзагруженная страница начинается для посетителя с момента показа, а не загрузки.
  var start = (nav && nav.activationStart) || 0;
  if (nav && nav.responseStart > 0) v.t = Math.max(0, nav.responseStart - start);
  observe('paint', function (e) {
    if (e.name === 'first-contentful-paint' && e.startTime < hiddenAt) v.f = Math.max(0, e.startTime - start);
  });
  observe('largest-contentful-paint', function (e) {
    if (e.startTime < hiddenAt) v.l = Math.max(0, e.startTime - start);
  });
  // CLS — самое большое «окно» сдвигов: подряд с разрывом меньше секунды и не дольше пяти.
  var cls = 0, win = 0, first = 0, last = 0;
  if (P.supportedEntryTypes && P.supportedEntryTypes.indexOf('layout-shift') >= 0) v.c = 0;
  observe('layout-shift', function (e) {
    if (e.hadRecentInput) return;
    if (win && e.startTime - last < 1000 && e.startTime - first < 5000) {
      win += e.value;
    } else {
      win = e.value;
      first = e.startTime;
    }
    last = e.startTime;
    if (win > cls) {
      cls = win;
      v.c = cls * 1000;
    }
  });
  // INP — самое долгое взаимодействие. У Google это 98-й перцентиль, но на одной странице
  // взаимодействий обычно меньше пятидесяти, и тогда это одно и то же.
  observe('event', function (e) {
    if (e.interactionId && !(e.duration <= (v.i || 0))) v.i = e.duration;
  }, { durationThreshold: 40 });
  function send() {
    if (sent) return;
    sent = true;
    var q = 'p=' + encodeURIComponent(location.pathname);
    for (var k in v) q += '&' + k + '=' + Math.round(v[k]);
    var url = '/_airlok/v?' + q;
    try {
      if (!(navigator.sendBeacon && navigator.sendBeacon(url))) fetch(url, { method: 'POST', keepalive: true });
    } catch (e) {}
  }
  addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') send();
  });
  addEventListener('pagehide', send);
})();
