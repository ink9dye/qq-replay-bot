/* Bounded backoff, explicitly dated last-success results, and a progress bar.
 *
 * 2026-11-22: resource_busy 不再当“失败”直接弹错——它只是“当前没有资源空档”，
 * 与 query_busy 一样重试到等待上限，页面用进度条显示百分比与预计剩余时间。
 */
(function () {
  'use strict';
  const native = window.fetch.bind(window), DEFAULT_LIMIT_MS = 600000, CARD_LIMIT_MS = 300000;
  // 各接口的初始估算（毫秒）。只用于进度条的起点，实际会随已用时间自我修正。
  const BASE_ESTIMATE_MS = { 'card-report': 75000, matrix: 45000, recordings: 30000, roster: 20000, 'cards-extra': 20000 };
  const DEFAULT_ESTIMATE_MS = 30000;
  const RESOURCE_RETRY_MS = 10000;
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
  function statusText(text) {
    const node = document.getElementById('status');
    if (node) node.textContent = text;
  }
  // ---- 进度条（不用额外样式表，保持单文件可回滚） ----
  let progressBox = null, progressFill = null, progressLabel = null;
  function ensureProgress() {
    if (progressBox) return;
    progressBox = document.createElement('div');
    progressBox.id = 'statistics-progress';
    progressBox.setAttribute('role', 'progressbar');
    progressBox.setAttribute('aria-valuemin', '0');
    progressBox.setAttribute('aria-valuemax', '100');
    progressBox.setAttribute('aria-valuenow', '0');
    progressBox.style.cssText = 'display:none;margin:8px 0 6px;max-width:520px;font-size:13px;color:#374151';
    const track = document.createElement('div');
    track.style.cssText = 'height:8px;border-radius:4px;background:#e5e7eb;overflow:hidden';
    progressFill = document.createElement('div');
    progressFill.style.cssText = 'height:100%;width:0;background:#2563eb;transition:width .3s ease';
    track.appendChild(progressFill);
    progressLabel = document.createElement('div');
    progressLabel.style.cssText = 'margin-top:4px;line-height:1.5';
    progressBox.appendChild(track); progressBox.appendChild(progressLabel);
  }
  function showProgress(percent, text) {
    ensureProgress();
    if (!progressBox.isConnected) {
      const host = document.getElementById('statistics-refresh-note');
      if (host && host.parentNode) host.parentNode.insertBefore(progressBox, host.nextSibling);
      else (document.querySelector('main') || document.body).prepend(progressBox);
    }
    progressBox.style.display = '';
    progressBox.setAttribute('aria-valuenow', String(percent));
    progressFill.style.width = percent + '%';
    progressLabel.textContent = text;
  }
  function hideProgress() {
    if (!progressBox) return;
    progressBox.style.display = 'none';
    progressBox.setAttribute('aria-valuenow', '0');
    progressFill.style.width = '0%';
    progressLabel.textContent = '';
  }
  function pause(ms, signal) { return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); if (signal) signal.removeEventListener('abort', abort); reject(new DOMException('已取消','AbortError')); };
    const timer = setTimeout(() => { if (signal) signal.removeEventListener('abort', abort); resolve(); }, ms);
    if (signal) { if (signal.aborted) abort(); else signal.addEventListener('abort', abort, {once:true}); }
  }); }
  // 进度文字：预计时间没到就报百分比 + 剩余秒数；超过了就不要再编一个越来越大的 ETA，
  // 直接说“即将完成”，避免用户看到“预计还需”从 20 秒涨到 40 秒。
  function progressLine(started, estimate, prefix) {
    const elapsed = Date.now() - started;
    if (elapsed > estimate) {
      const secs = Math.round(elapsed / 1000);
      return { percent: 95, text: prefix + ' 95%（已等待 ' + secs + ' 秒，即将完成）' };
    }
    const percent = Math.max(1, Math.min(95, Math.round(elapsed / estimate * 100)));
    const eta = Math.max(1, Math.round((estimate - elapsed) / 1000));
    return { percent, text: prefix + ' ' + percent + '%，预计还需 ' + eta + ' 秒' };
  }
  const delay = n => Math.min(30000, 3000 * Math.pow(1.5, Math.min(n, 7)));
  window.fetch = async function(input, options) {
    const raw = typeof input === 'string' ? input : input.url;
    const url = new URL(raw, location.href), kind = url.pathname.split('/').pop();
    if (url.origin !== location.origin || !['roster','recordings','card-report','matrix','cards-extra'].includes(kind)) return native(input, options);
    const signal = (options && options.signal) || (input && input.signal);
    if (signal) signal.addEventListener('abort', hideProgress, { once: true });
    const waitLimit = Number(window.__statisticsWaitLimitMs) || (kind === 'card-report' ? CARD_LIMIT_MS : DEFAULT_LIMIT_MS);
    const started = Date.now();
    const current = ++generation, form = formGeneration, deadline = started + waitLimit;
    let estimate = BASE_ESTIMATE_MS[kind] || DEFAULT_ESTIMATE_MS;
    const valid = () => current === generation && form === formGeneration && !(signal && signal.aborted);
    for (let attempt = 0;; attempt++) {
      if (!valid()) { hideProgress(); throw new DOMException('条件已变，请重新查询','AbortError'); }
      const response = await native(input, options);
      if (!valid()) { hideProgress(); throw new DOMException('条件已变，请重新查询','AbortError'); }
      if (response.status === 503) {
        let payload; try { payload = await response.clone().json(); } catch (_) {}
        // 服务端会带 estimate_ms（同一 key 上一轮耗时）和 queue_position；
        // 有就用真实值，没有才退回按接口类型的默认估算。
        const serverEstimate = Number(payload && payload.estimate_ms);
        if (serverEstimate > 0) estimate = serverEstimate;
        const ahead = Number(payload && payload.queue_position) || 0;
        const aheadText = ahead > 1 ? '；前面还有 ' + (ahead - 1) + ' 个任务' : '';
        if (payload && payload.code === 'query_busy') {
          if (Date.now() >= deadline) {
            hideProgress();
            const msg = '统计等待超时（已等待 ' + Math.round((Date.now() - started) / 1000) +
              ' 秒）：任务可能仍在计算，请稍后重试；不要重复提交。';
            statusText(msg); notice(msg);
            throw new Error(msg);
          }
          const step = progressLine(started, estimate, '正在计算');
          statusText(step.text);
          showProgress(step.percent, step.text + aheadText + '；完成后自动显示，请勿重复提交。');
          await pause(delay(attempt), signal); continue;
        }
        if (payload && payload.code === 'resource_busy') {
          // 资源预算不足不是“失败”，只是这一轮没有空档：等一会儿再试，
          // 服务端 error 冷却 30 秒，10 秒一次重试会在冷却结束后重新入队。
          if (Date.now() >= deadline) {
            hideProgress();
            const msg = '统计暂时没有空闲资源（已等待 ' + Math.round((Date.now() - started) / 1000) +
              ' 秒）：请稍后刷新页面重试；不要重复提交。';
            statusText(msg); notice(msg);
            throw new Error(msg);
          }
          const step = progressLine(started, estimate, '排队等待空闲资源');
          statusText(step.text);
          showProgress(step.percent, step.text + '；有空档会自动开始，请勿重复提交。');
          await pause(RESOURCE_RETRY_MS, signal); continue;
        }
        if (payload && payload.code === 'civil_snapshot_unavailable') {
          hideProgress();
          const text = '内战数据暂不可用（快照缺失或正在刷新），请稍后重试。';
          statusText(text); notice(text); return response;
        }
      }
      const state = response.headers.get('X-Statistics-State');
      if (response.ok && ['refreshing','stale'].includes(state)) {
        hideProgress();
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
      } else if (response.ok) { hideProgress(); notice(''); }
      else if (response.status === 503) {
        hideProgress();
        const text = '本次等待已结束，服务端任务不会重复提交；请稍后点击查询。';
        statusText(text); notice(text);
      } else {
        hideProgress();
      }
      return response;
    }
  };
})();
