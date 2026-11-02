/* 统计页统一“正在运行”提示：同一页面只出现一个。
 *
 * 优先用页面自己的状态文字（[role=status]，可见且文案是“正在/查询中…”）：给它补转圈，
 * 不再叠加浮层；只有页内提示不可见（弹窗、滚出屏幕、没有状态位置）时才用
 * 顶部进度条 + 底部胶囊（含“已 N 秒”）。
 * 页面可用 body[data-busy] 或 StatisticsBusy.set('…') 指定更准确的浮层文案。
 */
(function () {
  if (window.StatisticsBusy) return;

  var STATE = { count: 0, started: 0, timer: 0, label: '', inline: null };
  var wrap = null, bar = null, pill = null, labelNode = null, clockNode = null;

  var CSS = [
    '.sb-wrap{position:fixed;inset:0 0 auto 0;z-index:2147483000;pointer-events:none;',
    'font:13px/1.5 "Microsoft YaHei",system-ui,-apple-system,sans-serif}',
    '.sb-bar{height:3px;background:linear-gradient(90deg,#6f9a58,#c9e787,#6f9a58);background-size:220% 100%;',
    'transform:scaleX(0);transform-origin:0 50%;transition:transform .25s ease;animation:sb-slide 1.15s linear infinite}',
    '.sb-wrap.on .sb-bar{transform:scaleX(1)}',
    '@keyframes sb-slide{0%{background-position:0 50%}100%{background-position:220% 50%}}',
    '.sb-pill{position:fixed;left:50%;bottom:max(18px,env(safe-area-inset-bottom));transform:translateX(-50%);',
    'display:none;align-items:center;gap:9px;max-width:min(86vw,520px);padding:9px 16px;border-radius:999px;',
    'background:rgba(23,63,50,.94);color:#eef6e6;box-shadow:0 8px 24px rgba(0,0,0,.24);',
    'font-size:13px;line-height:1.4;pointer-events:none}',
    '.sb-wrap.on .sb-pill{display:flex}',
    '.sb-spin{width:13px;height:13px;flex:none;border-radius:50%;border:2px solid rgba(255,255,255,.35);',
    'border-top-color:#c9e787;animation:sb-rot .8s linear infinite}',
    '@keyframes sb-rot{to{transform:rotate(360deg)}}',
    '.sb-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.sb-clock{flex:none;font-variant-numeric:tabular-nums;color:#c9e787}',
    '@media (prefers-reduced-motion:reduce){.sb-bar,.sb-spin{animation:none}}'
  ].join('');

  function build() {
    if (wrap) return;
    var style = document.createElement('style');
    style.id = 'statistics-busy-style';
    style.textContent = CSS;
    (document.head || document.documentElement).appendChild(style);
    wrap = document.createElement('div');
    wrap.className = 'sb-wrap';
    wrap.setAttribute('aria-hidden', 'true');
    bar = document.createElement('div');
    bar.className = 'sb-bar';
    pill = document.createElement('div');
    pill.className = 'sb-pill';
    pill.setAttribute('role', 'status');
    pill.setAttribute('aria-live', 'polite');
    var spin = document.createElement('span');
    spin.className = 'sb-spin';
    labelNode = document.createElement('span');
    labelNode.className = 'sb-label';
    clockNode = document.createElement('span');
    clockNode.className = 'sb-clock';
    pill.appendChild(spin);
    pill.appendChild(labelNode);
    pill.appendChild(clockNode);
    wrap.appendChild(bar);
    wrap.appendChild(pill);
    document.body.appendChild(wrap);
  }

  function looksBusy(text) {
    return !!text && text.length <= 48 &&
      /(正在|读取|查询|筛选中|加载|统计中|打包|生成|处理中|保存中)/.test(text);
  }

  function pickLabel() {
    if (window.StatisticsBusy && window.StatisticsBusy.fixed) return window.StatisticsBusy.fixed;
    var body = document.body;
    if (body && body.dataset && body.dataset.busy) return body.dataset.busy;
    var nodes = document.querySelectorAll('[role="status"]');
    for (var i = 0; i < nodes.length; i += 1) {
      var text = (nodes[i].textContent || '').replace(/\s+/g, ' ').trim();
      if (looksBusy(text)) return text;
    }
    return '正在处理…';
  }

  // 同一页面只保留一个“正在运行”提示：
  // 页面自己的状态文字（[role=status] 且可见、文案是“正在…”）优先，此时不再显示浮层；
  // 只有在页内提示不可见（弹窗、滚动到屏外、没有状态位置）时才用顶部进度条 + 底部胶囊。
  function visibleInlineStatus() {
    var nodes = document.querySelectorAll('[role="status"]');
    var height = window.innerHeight || document.documentElement.clientHeight || 0;
    for (var i = 0; i < nodes.length; i += 1) {
      var node = nodes[i];
      var text = (node.textContent || '').replace(/\s+/g, ' ').trim();
      if (!looksBusy(text)) continue;
      var rect = node.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0 && rect.bottom > 8 && rect.top < height - 8) {
        return node;
      }
    }
    return null;
  }

  function paint() {
    if (!wrap || !STATE.count) return;
    var inline = visibleInlineStatus();
    // 页内已有可见提示：只留它，并补上转圈样式；否则才显示浮层。
    wrap.classList.toggle('on', !inline);
    if (inline !== STATE.inline) {
      if (STATE.inline) STATE.inline.classList.remove('busy');
      if (inline) inline.classList.add('busy');
      STATE.inline = inline;
    }
    if (inline) return;
    var label = pickLabel();
    var seconds = Math.floor((Date.now() - STATE.started) / 1000);
    if (label !== STATE.label) {
      STATE.label = label;
      labelNode.textContent = label;
    }
    clockNode.textContent = seconds >= 1 ? '已 ' + seconds + ' 秒' : '';
  }

  function begin() {
    if (!document.body) return;
    build();
    if (STATE.count === 0) {
      STATE.started = Date.now();
      STATE.label = '';
      if (document.body.dataset) document.body.dataset.loading = '1';
      STATE.timer = window.setInterval(paint, 250);
    }
    STATE.count += 1;
    paint();
  }

  function end() {
    STATE.count = Math.max(0, STATE.count - 1);
    if (STATE.count === 0) {
      window.clearInterval(STATE.timer);
      STATE.timer = 0;
      if (wrap) wrap.classList.remove('on');
      if (STATE.inline) {
        STATE.inline.classList.remove('busy');
        STATE.inline = null;
      }
      if (document.body && document.body.dataset) delete document.body.dataset.loading;
    }
  }

  window.StatisticsBusy = {
    begin: begin,
    end: end,
    fixed: '',
    set: function (text) { window.StatisticsBusy.fixed = text || ''; paint(); },
    clear: function () { window.StatisticsBusy.fixed = ''; }
  };

  var native = window.fetch;
  if (typeof native === 'function') {
    window.fetch = function () {
      var url = typeof arguments[0] === 'string' ? arguments[0]
        : (arguments[0] && arguments[0].url) || '';
      // 卡图缩略图会并发几十个请求，不参与“正在运行”提示，避免闪个不停。
      if (url.indexOf('/statistics/card-image/') >= 0 || url.indexOf('/statistics/assets/') >= 0) {
        return native.apply(window, arguments);
      }
      begin();
      var promise;
      try {
        promise = native.apply(window, arguments);
      } catch (error) {
        end();
        throw error;
      }
      if (promise && typeof promise.then === 'function') {
        promise.then(end, end);
      } else {
        end();
      }
      return promise;
    };
  }
}());
