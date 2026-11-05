/* Bounded backoff and explicitly dated last-success results. */
(function () {
  'use strict';
  const native = window.fetch.bind(window), limitMs = 600000;
  let generation = 0, formGeneration = 0;
  for (const event of ['input','change','reset']) document.addEventListener(event, () => { formGeneration++; }, true);
  function notice(text) {
    let n = document.getElementById('statistics-refresh-note');
    if (!n) {
      n = document.createElement('p'); n.id = 'statistics-refresh-note'; n.setAttribute('role','status');
      n.style.cssText = 'padding:10px 14px;background:#fff5df;color:#79541b;border-radius:8px';
      (document.querySelector('main') || document.body).prepend(n);
    }
    n.hidden = !text; n.textContent = text;
  }
  function pause(ms, signal) { return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); if (signal) signal.removeEventListener('abort', abort); reject(new DOMException('已取消','AbortError')); };
    const timer = setTimeout(() => { if (signal) signal.removeEventListener('abort', abort); resolve(); }, ms);
    if (signal) { if (signal.aborted) abort(); else signal.addEventListener('abort', abort, {once:true}); }
  }); }
  const delay = n => Math.min(30000, 3000 * Math.pow(1.5, Math.min(n, 7)));
  window.fetch = async function(input, options) {
    const raw = typeof input === 'string' ? input : input.url;
    const url = new URL(raw, location.href), kind = url.pathname.split('/').pop();
    if (url.origin !== location.origin || !['roster','recordings','card-report','matrix','cards-extra'].includes(kind)) return native(input, options);
    const signal = (options && options.signal) || (input && input.signal);
    const current = ++generation, form = formGeneration, deadline = Date.now() + limitMs;
    const valid = () => current === generation && form === formGeneration && !(signal && signal.aborted);
    for (let attempt = 0;; attempt++) {
      if (!valid()) throw new DOMException('条件已变，请重新查询','AbortError');
      const response = await native(input, options);
      if (!valid()) throw new DOMException('条件已变，请重新查询','AbortError');
      if (response.status === 503 && Date.now() < deadline) {
        let payload; try { payload = await response.clone().json(); } catch (_) {}
        if (payload && payload.code === 'query_busy') {
          notice('正在准备统计结果，耗时较长时会自动继续等待；已有结果会保留。');
          await pause(delay(attempt), signal); continue;
        }
      }
      const state = response.headers.get('X-Statistics-State');
      if (response.ok && ['refreshing','stale'].includes(state)) {
        const stamp = Number(response.headers.get('X-Statistics-Saved-At'));
        notice('当前显示 ' + new Date(stamp * 1000).toLocaleString('zh-CN') + ' 保存的上次统计结果，等待更新。');
        (async () => {
          for (let attempt = 0; Date.now() < deadline; attempt++) {
            await pause(delay(attempt), signal);
            if (!valid() || document.hidden) return;
            const next = await native(input, options);
            if (!valid()) return;
            if (next.ok && Number(next.headers.get('X-Statistics-Saved-At')) > stamp) {
              notice('统计已更新。');
              window.dispatchEvent(new CustomEvent('statistics-result-ready', {detail:{kind}})); return;
            }
          }
          if (valid()) notice('仍在等待更新，已保留上次结果；可稍后点击查询。');
        })().catch(() => {});
      } else if (response.ok) notice('');
      else if (response.status === 503) notice('本次等待已结束，服务端任务不会重复提交；请稍后点击查询。');
      return response;
    }
  };
})();
