/* Visible candidates shared by card and deck inputs; uses existing page data. */
(() => {
  'use strict';
  const attached = new WeakMap();
  function attach(input, options) {
    if (attached.has(input)) return attached.get(input);
    const wrap = document.createElement('span'), list = document.createElement('ul');
    wrap.className = 'statistics-candidate-wrap';
    list.className = 'statistics-candidates';
    list.id = input.id + '-candidates';
    list.setAttribute('role', 'listbox');
    list.setAttribute('aria-label', options.label || '输入候选');
    list.hidden = true;
    input.before(wrap); wrap.append(input, list);
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-controls', list.id);
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('autocomplete', 'off');
    let visible = [], active = -1, composing = false;
    const close = () => {
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      active = -1;
    };
    function choose(index) {
      const item = visible[index];
      if (!item) return;
      if (options.choose) options.choose(item);
      else {
        input.value = item.value;
        input.dispatchEvent(new Event('input', {bubbles:true}));
        input.dispatchEvent(new Event('change', {bubbles:true}));
      }
      close(); input.focus(); close();
    }
    function highlight() {
      for (const [index, row] of [...list.children].entries()) {
        row.setAttribute('aria-selected', String(index === active));
      }
      if (active >= 0) {
        input.setAttribute('aria-activedescendant', list.id + '-' + active);
        list.children[active].scrollIntoView({block:'nearest'});
      }
    }
    function render() {
      if (document.activeElement !== input || composing) return;
      const term = input.value.trim().toLocaleLowerCase();
      visible = options.items().filter(item => !term ||
        (item.label + ' ' + item.value).toLocaleLowerCase().includes(term)).slice(0, 40);
      list.replaceChildren(); active = -1;
      for (const [index, item] of visible.entries()) {
        const row = document.createElement('li'), name = document.createElement('span');
        row.id = list.id + '-' + index;
        row.setAttribute('role', 'option'); row.setAttribute('aria-selected', 'false');
        name.textContent = item.label; row.append(name);
        if (item.detail) { const detail = document.createElement('small'); detail.textContent = item.detail; row.append(detail); }
        row.addEventListener('pointerdown', event => event.preventDefault());
        row.addEventListener('click', event => { event.preventDefault(); choose(index); });
        list.append(row);
      }
      if (!visible.length) {
        const row = document.createElement('li'); row.className = 'candidate-note';
        row.textContent = input.dataset.candidateError ||
          (input.dataset.candidateLoading === '1' ? '正在加载候选…' : '没有匹配的候选');
        list.append(row);
      }
      list.hidden = false; input.setAttribute('aria-expanded', 'true');
      input.removeAttribute('aria-activedescendant');
    }
    input.addEventListener('input', render);
    input.addEventListener('focus', render);
    input.addEventListener('blur', close);
    input.addEventListener('compositionstart', () => { composing = true; close(); });
    input.addEventListener('compositionend', () => { composing = false; render(); });
    input.addEventListener('keydown', event => {
      if (composing || event.isComposing) return;
      if (event.key === 'Escape') { close(); return; }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); event.stopImmediatePropagation();
        if (list.hidden) render();
        if (!visible.length) return;
        active = event.key === 'ArrowDown' ? (active + 1) % visible.length :
          (active < 0 ? visible.length - 1 : (active - 1 + visible.length) % visible.length);
        highlight();
      } else if (event.key === 'Enter' && !list.hidden && visible.length) {
        if (options.freeText && active < 0) { close(); return; }
        event.preventDefault(); event.stopImmediatePropagation(); choose(active < 0 ? 0 : active);
      }
    }, true);
    const control = {render, close}; attached.set(input, control); return control;
  }
  function enhance() {
    for (const input of document.querySelectorAll('input[list], input[data-candidate-source]')) {
      if (attached.has(input)) continue;
      const source = document.getElementById(input.getAttribute('list') || input.dataset.candidateSource);
      if (!source) continue;
      const freeText = /deck/i.test(source.id);
      if (freeText) input.title = '可输入卡组名的一部分，匹配所有名称包含它的卡组；也可选择候选';
      const control = attach(input, {freeText, items: () => [...source.options].filter(o => o.value).map(o => ({
        value:o.value, label:o.label || o.textContent || o.value
      }))});
      input.removeAttribute('list');
      new MutationObserver(control.render).observe(source, {subtree:true, childList:true, attributes:true, characterData:true});
      new MutationObserver(control.render).observe(input, {attributes:true, attributeFilter:['data-candidate-loading','data-candidate-error']});
    }
  }
  // Add explicit batch confirmation without changing the existing text picker.
  function deckBatch(id, picks, options = {}) {
    const input = document.getElementById(id);
    const row = document.createElement('span'), tools = document.createElement('span'), confirm = document.createElement('button');
    const clear = document.createElement('button'), hint = document.createElement('small');
    const chips = options.chips || document.createElement('span');
    row.style.cssText = 'display:flex;align-items:center;gap:6px;min-width:0';
    tools.style.cssText = 'display:flex;flex-shrink:0;gap:4px';
    chips.classList.add('chips');
    chips.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;margin-top:6px';
    chips.setAttribute('aria-live', 'polite');
    hint.setAttribute('role', 'status');
    hint.style.cssText = 'display:block;font-size:12px;font-weight:normal;overflow-wrap:anywhere';
    confirm.type = clear.type = 'button';
    confirm.id = id + '-confirm'; clear.id = id + '-clear';
    confirm.textContent = '确定'; clear.textContent = '清空';
    confirm.title = '将当前双方排名范围内匹配的完整卡组名全部加入筛选';
    tools.append(confirm, clear);
    input.before(row); row.append(input, tools);
    row.after(chips, hint);
    let version = 0;
    const confirmed = new Set();
    const changed = () => { if (options.changed) options.changed(); };
    const render = options.render || (() => {
      chips.replaceChildren();
      for (const name of picks) {
        const chip = document.createElement('span'), remove = document.createElement('button');
        chip.className = 'chip';
        chip.append(document.createTextNode(name + ' '));
        remove.type = 'button'; remove.textContent = '×';
        remove.setAttribute('aria-label', '移除 ' + name);
        remove.onclick = () => { picks.delete(name); render(); changed(); };
        chip.append(remove); chips.append(chip);
      }
    });
    function invalidate() { version++; confirm.disabled = false; input.dataset.batchPending = '0'; }
    function reset() { invalidate(); picks.clear(); confirmed.clear(); input.value = ''; hint.textContent = ''; render(); }
    for (const button of [confirm, clear]) {
      // Keep the old recording blur handler from adding the unexpanded fragment.
      button.addEventListener('pointerdown', event => event.preventDefault());
    }
    confirm.onclick = async () => {
      const token = ++version, term = input.value.trim();
      const ranges = ['self_range', 'opponent_range'].map(key => document.getElementById(key).value);
      confirm.disabled = true; input.dataset.batchPending = '1'; hint.textContent = '正在查找匹配卡组…';
      const query = new URLSearchParams({candidates:'1', q:term, self_range:ranges[0],
        opponent_range:ranges[1], page:options.page || 'cards'});
      try {
        const result = await window.StatisticsSamples.api('groups?' + query);
        if (token !== version || input.value.trim() !== term) return;
        if (result.version !== 1) throw Error('统计服务尚未加载卡组批量选择，请稍后刷新。');
        if (!result.complete) throw Error('部分玩家排名尚未核验，请稍后重试；未加入不完整名单。');
        const names = result[options.side || 'deck'];
        if (!Array.isArray(names)) throw Error('卡组候选格式无效，请重试。');
        if (!names.length) { hint.textContent = '当前双方排名范围内没有匹配卡组。'; return; }
        if (new Set([...picks, ...names]).size > 100) throw Error('最多选择100个卡组，请缩小关键词或排名范围。');
        for (const name of names) { picks.add(name); confirmed.add(name); }
        input.value = ''; render(); input.blur(); changed();
        hint.textContent = '已加入匹配的 ' + names.length + ' 个卡组，当前共选 ' + picks.size + ' 个；点查询或应用筛选生效。';
        if(result._query_refresh)hint.textContent+=' 候选更新于 '+new Date(result._query_refresh.saved_at*1000).toLocaleTimeString('zh-CN')+'；录像按实际筛选。';
      } catch (error) {
        if (token === version) hint.textContent = error.message || '卡组查询失败，请重试。';
      } finally {
        if (token === version) { confirm.disabled = false; input.dataset.batchPending = '0'; }
      }
    };
    clear.onclick = () => { reset(); changed(); };
    input.addEventListener('input', () => { invalidate(); hint.textContent = ''; });
    for (const key of ['self_range', 'opponent_range']) {
      document.getElementById(key).addEventListener('change', () => {
        invalidate(); hint.textContent = '范围已改；可点确定重新加入匹配卡组，已有选择保留。';
      });
    }
    return {reset, render, exact: () => [...picks].every(name => confirmed.has(name)),
      restore: names => { for (const name of names) { picks.add(name); confirmed.add(name); } render(); }};
  }
  window.StatisticsAutocomplete = {attach, enhance, deckBatch};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', enhance);
  else enhance();
})();
