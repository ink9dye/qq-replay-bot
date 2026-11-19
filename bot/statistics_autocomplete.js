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
  function deckFilter(id, submit, options = {}) {
    const input = document.getElementById(id);
    const source = document.getElementById(input.getAttribute('list'));
    const tools = document.createElement('span');
    const confirm = document.createElement('button');
    const no60 = document.createElement('button');
    tools.className = 'statistics-deck-actions';
    confirm.type = no60.type = 'button';
    confirm.textContent = '确定';
    no60.title = '排除名称以 60 开头的卡组';
    const excluded = () => no60.getAttribute('aria-pressed') === 'true';
    const setExcluded = value => {
      no60.setAttribute('aria-pressed', String(value));
      no60.textContent = value ? '清除60 · 已开启' : '清除60';
    };
    setExcluded(false);
    const control = attach(input, {
      label: '卡组候选',
      freeText: true,
      items: () => {
        const names = options.names ? options.names() : [...source.options].map(o => o.value);
        return names.filter(name => name && (!excluded() || !name.startsWith('60')))
          .map(name => ({value: name, label: name}));
      },
      choose: item => {
        input.value = item.value;
        if (options.choose) options.choose(item.value);
      }
    });
    input.removeAttribute('list');
    if (source) new MutationObserver(control.render).observe(source, {subtree:true, childList:true, attributes:true});
    new MutationObserver(control.render).observe(input, {
      attributes:true, attributeFilter:['data-candidate-loading','data-candidate-error']
    });
    input.parentElement.after(tools);
    tools.append(confirm, no60);
    confirm.onclick = submit;
    no60.onclick = () => { setExcluded(!excluded()); control.close(); submit(); };
    input.addEventListener('keydown', event => {
      if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      submit();
    });
    return {
      tools, excluded, setExcluded,
      term: () => input.value.trim(),
      reset: () => { input.value = ''; setExcluded(false); control.close(); },
      append(params, key, encode = value => value) {
        const term = input.value.trim();
        if (term) params.append(key, encode(term));
        if (excluded()) params.set(key + '_no60', '1');
      }
    };
  }

  function deckBatch(id, picks, submit, chips = document.createElement('span')) {
    const input = document.getElementById(id);
    chips.classList.add('chips');
    chips.setAttribute('aria-live', 'polite');
    let exact = true;
    function render() {
      chips.replaceChildren();
      for (const name of picks) {
        const chip = document.createElement('span'), remove = document.createElement('button');
        chip.className = 'chip';
        chip.append(document.createTextNode((name ?? '卡组未识别') + ' '));
        remove.type = 'button';
        remove.textContent = '×';
        remove.setAttribute('aria-label', '移除 ' + name);
        remove.onclick = () => { picks.delete(name); render(); submit(); };
        chip.append(remove);
        chips.append(chip);
      }
    }
    const filter = deckFilter(id, submit, {choose: name => {
      if (picks.size >= 100 && !picks.has(name)) return;
      picks.add(name);
      input.value = '';
      render();
    }});
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.textContent = '清空';
    filter.tools.append(clear);
    filter.tools.after(chips);
    function reset() { filter.reset(); picks.clear(); exact = true; render(); }
    clear.onclick = () => { reset(); submit(); };
    return {...filter, reset,
      exact: () => !filter.term() && (!picks.size || exact),
      restore: (names, exactMatch) => { exact = exactMatch; for (const name of names) picks.add(name); render(); }
    };
  }
  window.StatisticsAutocomplete = {attach, enhance, deckFilter, deckBatch};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', enhance);
  else enhance();
})();
