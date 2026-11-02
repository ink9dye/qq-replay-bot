/* 统计页“正在运行”提示：只用页面自己的状态文字，不额外加浮层。
 *
 * 做法：包一层 fetch；有请求在飞时，给页面上可见的 [role=status]「正在…/查询中…」
 * 补一个转圈样式（.busy），请求结束再移除。页面没有状态文字时不显示任何东西，
 * 避免同一个界面出现两套提示或一个泛泛的“正在处理…”。
 */
(function () {
  if (window.StatisticsBusy) return;

  var STATE = { count: 0, node: null, timer: 0 };

  function looksBusy(text) {
    return !!text && text.length <= 48 &&
      /(正在|读取|查询|筛选中|加载|统计中|打包|生成|处理中|保存中)/.test(text);
  }

  function visibleStatus() {
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
    var node = STATE.count > 0 ? visibleStatus() : null;
    if (node === STATE.node) return;
    if (STATE.node) STATE.node.classList.remove('busy');
    if (node) node.classList.add('busy');
    STATE.node = node;
  }

  function begin() {
    STATE.count += 1;
    paint();
    if (!STATE.timer) STATE.timer = window.setInterval(paint, 400);
  }

  function end() {
    STATE.count = Math.max(0, STATE.count - 1);
    if (STATE.count === 0 && STATE.timer) {
      window.clearInterval(STATE.timer);
      STATE.timer = 0;
    }
    paint();
  }

  // set/clear 保留给页面显式控制；当前没有浮层文案，保留空实现以兼容旧调用。
  window.StatisticsBusy = {
    begin: begin,
    end: end,
    set: function () {},
    clear: function () {}
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
