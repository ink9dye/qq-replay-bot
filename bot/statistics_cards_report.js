/* 卡片对比页：一次提交一个快照，旧响应永不复原新条件。 */
(() => {
'use strict';
const $ = (id) => document.getElementById(id);
const SS = window.StatisticsSamples;
const el = SS.el;
const state = { request: 0, controller: null, result: null, groupsReady: false };

function createPicker(ids) {
  const picker = { cards: new Map(), suggest: [], active: -1, version: 0, timer: 0 };
  const input = $(ids.input);
  const list = $(ids.list);
  const chips = $(ids.chips);
  const hint = $(ids.hint);
  const wrap = el('span', undefined, 'statistics-candidate-wrap');
  input.before(wrap);
  wrap.append(input, list);
  list.classList.add('statistics-candidates');

  picker.hint = () => {
    hint.textContent = ids.hintText.replace('{n}', picker.cards.size);
  };
  picker.render = () => {
    chips.replaceChildren();
    for (const [code, name] of picker.cards) {
      const chip = el('span', undefined, 'chip');
      chip.append(el('span', name || ('卡号 ' + code)));
      const remove = el('button', '×', 'chip-x');
      remove.type = 'button';
      remove.setAttribute('aria-label', '移除 ' + (name || code));
      remove.onclick = () => picker.remove(code);
      chip.append(remove);
      chips.append(chip);
    }
    picker.hint();
  };
  picker.add = (card) => {
    if (!card || !card.code) return;
    const code = Number(card.code);
    if (!picker.cards.has(code)) picker.cards.set(code, card.name || '');
    picker.render();
  };
  picker.remove = (code) => {
    picker.cards.delete(code);
    picker.render();
    input.focus();
  };
  picker.codes = () => [...picker.cards.keys()];
  picker.close = () => {
    picker.suggest = [];
    picker.active = -1;
    list.hidden = true;
    list.replaceChildren();
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  };
  picker.message = (text) => {
    picker.close();
    const item = el('li', text, 'candidate-note');
    item.setAttribute('role', 'status');
    list.append(item);
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  };
  picker.renderSuggest = () => {
    list.replaceChildren();
    picker.suggest.forEach((card, index) => {
      const item = el('li', undefined, 'suggest-item' + (index === picker.active ? ' active' : ''));
      item.id = ids.list + '-' + index;
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', index === picker.active ? 'true' : 'false');
      item.append(el('span', card.name || '卡名暂缺'), el('span', '#' + card.code, 'code'));
      item.onmousedown = (event) => {
        event.preventDefault();
        picker.add(card);
        picker.close();
        input.value = '';
        input.focus();
      };
      list.append(item);
    });
    if (picker.suggest.length) {
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      if (picker.active >= 0) input.setAttribute('aria-activedescendant', ids.list + '-' + picker.active);
    } else {
      picker.close();
    }
  };
  picker.search = async (term, version = ++picker.version) => {
    const text = String(term || '').trim();
    if (!text) {
      picker.close();
      return;
    }
    picker.message('正在查找候选…');
    let payload = { cards: [] };
    try {
      payload = await SS.api('card-search?q=' + encodeURIComponent(text));
    } catch (_) {
      if (version === picker.version) picker.message('候选暂不可用，重新输入或点击输入框重试。');
      return;
    }
    if (version !== picker.version) return;   // 旧候选绝不回填
    picker.suggest = (payload.cards || []).filter((card) => card && card.code);
    picker.active = picker.suggest.length ? 0 : -1;
    if (picker.suggest.length) picker.renderSuggest();
    else picker.message('没有匹配的卡片，请换用卡名或卡号。');
  };
  picker.reset = () => {
    picker.cards.clear();
    picker.version++;
    picker.close();
    input.value = '';
    picker.render();
  };

  input.addEventListener('input', (event) => {
    clearTimeout(picker.timer);
    const version = ++picker.version;
    const value = input.value.trim();
    if (!value) {
      picker.close();
      return;
    }
    picker.close();
    if (!event.isComposing) picker.timer = setTimeout(() => picker.search(value, version), 150);
  });
  input.addEventListener('compositionend', () => input.dispatchEvent(new Event('input')));
  input.addEventListener('focus', () => {
    if (picker.suggest.length) picker.renderSuggest();
    else if (input.value.trim()) {clearTimeout(picker.timer); picker.search(input.value);}
  });
  input.addEventListener('blur', () => {
    setTimeout(() => {
      if (document.activeElement !== input) {clearTimeout(picker.timer); picker.version++; picker.close();}
    }, 150);
  });
  input.addEventListener('keydown', (event) => {
    if (event.isComposing) return;
    if (event.key === 'ArrowDown' && picker.suggest.length) {
      event.preventDefault();
      picker.active = (picker.active + 1) % picker.suggest.length;
      picker.renderSuggest();
    } else if (event.key === 'ArrowUp' && picker.suggest.length) {
      event.preventDefault();
      picker.active = (picker.active - 1 + picker.suggest.length) % picker.suggest.length;
      picker.renderSuggest();
    } else if (event.key === 'Enter') {
      if (!list.hidden && picker.active >= 0) {
        event.preventDefault();
        picker.add(picker.suggest[picker.active]);
        picker.close();
        input.value = '';
      }
    } else if (event.key === 'Escape') {
      clearTimeout(picker.timer); picker.version++;
      picker.close();
    } else if (event.key === 'Backspace' && !input.value && picker.cards.size) {
      picker.remove([...picker.cards.keys()].pop());
    }
  });

  picker.render();
  return picker;
}

const targets = createPicker({
  input: 'card-input', list: 'suggest', chips: 'chips', hint: 'cards-hint',
  hintText: '输入卡名或卡号后从候选选择；不选卡时按下面条件列出整表。已选 {n} 张。',
});
const excludes = createPicker({
  input: 'exclude-input', list: 'exclude-suggest', chips: 'exclude-chips', hint: 'exclude-hint',
  hintText: '起手不含这些卡；多张时要求全部都不在起手（AND）。起手未知或不完整的小局不计入。已排除 {n} 张。',
});

/* 常用手坑/圣枪套餐：与截图清单一致，按卡号加入，已选不会重复。 */
const HAND_TRAP_PRESET = [
  { code: 73642296, name: '屋敷童' },
  { code: 14558127, name: '灰流丽' },
  { code: 27204311, name: '原始生命态 尼比鲁' },
  { code: 97268402, name: '效果遮蒙者' },
  { code: 10045474, name: '无限泡影' },
  { code: 23434538, name: '增殖的G' },
  { code: 42141493, name: '欢聚友伴·茸茸长尾山雀' },
  { code: 84192580, name: '欢聚友伴·抖抖海月水母' },
  { code: 34267821, name: '古遗物-圣枪' },
  { code: 87126721, name: '欢聚友伴·喵喵豹猫' },
  { code: 27243130, name: '禁忌的圣枪' },
  { code: 52038441, name: '朔夜时雨' },
  { code: 67750322, name: '骷髅大王' },
  { code: 101307021, name: '灾祸的根绝者' },
];
const presetButton = $('handtrap-preset');
if (presetButton) {
  presetButton.addEventListener('click', () => {
    for (const card of HAND_TRAP_PRESET) targets.add(card);
  });
}
const NIBIRU_COMBO_PRESET = [
  [{ code: 10045474, name: '无限泡影' }, { code: 27204311, name: '原始生命态 尼比鲁' }],
  [{ code: 97268402, name: '效果遮蒙者' }, { code: 27204311, name: '原始生命态 尼比鲁' }],
];
const BOARD_BREAKER_PRESET = [
  { code: 101307067, name: '决斗者之力' },
  { code: 24203749, name: '天下独步之大义贼' },
  { code: 2729965, name: '宇宙性飓风' },
  { code: 48130397, name: '超融合' },
  { code: 98829635, name: '禁忌的圣冠' },
  { code: 98476659, name: '怠慢之壶' },
  { code: 30271097, name: '落胤与圣女' },
  { code: 66247039, name: '神圣心灵防护罩 -心灵之力-' },
  { code: 15693423, name: '颉颃胜负' },
  { code: 18144506, name: '鹰身女妖的羽毛扫' },
  { code: 46565218, name: '撒旦老人' },
  { code: 10000080, name: '拉之翼神龙-球体形' },
  { code: 102380, name: '熔岩魔神' },
];
const boardPresetButton = $('board-breaker-preset');
if (boardPresetButton) {
  boardPresetButton.addEventListener('click', () => {
    for (const card of BOARD_BREAKER_PRESET) targets.add(card);
  });
}
const clearButton = $('handtrap-clear');
if (clearButton) clearButton.addEventListener('click', () => {
  targets.reset();
  comboDefs.length = 0;
  renderComboDefs();
});

const comboPicker = createPicker({
  input: 'combo-input', list: 'combo-suggest', chips: 'combo-chips', hint: 'combo-hint',
  hintText: '至少选 2 张，起手必须同时抓到（AND）。已选 {n} 张。',
});
const comboDefs = [];

function renderComboDefs() {
  const box = $('combo-defs');
  box.replaceChildren();
  comboDefs.forEach((combo, index) => {
    const chip = el('span', undefined, 'combo-chip');
    const names = combo.cards.map(card => card.name || ('#' + card.code)).join(' + ');
    chip.title = names;
    chip.append(el('span', '组合' + (index + 1) + '：' + names));
    const remove = el('button', '×');
    remove.type = 'button';
    remove.setAttribute('aria-label', '移除组合' + (index + 1));
    remove.onclick = () => {
      comboDefs.splice(index, 1);
      renderComboDefs();
    };
    chip.append(remove);
    box.append(chip);
  });
}

$('combo-open').onclick = () => {
  const editor = $('combo-editor');
  editor.hidden = !editor.hidden;
  if (!editor.hidden) $('combo-input').focus();
};
$('combo-cancel').onclick = () => {
  $('combo-editor').hidden = true;
  $('combo-status').textContent = '';
};
$('combo-run').onclick = () => {
  const codes = comboPicker.codes();
  if (codes.length < 2) {
    $('combo-status').textContent = '组合至少需要 2 张卡。';
    return;
  }
  if (comboDefs.length >= 4) {
    $('combo-status').textContent = '最多设置 4 个组合。';
    return;
  }
  comboDefs.push({
    codes: [...codes],
    cards: [...comboPicker.cards].map(([code, name]) => ({ code, name })),
  });
  renderComboDefs();
  comboPicker.reset();
  $('combo-status').textContent = '';
  $('combo-editor').hidden = true;
};

function addComboCards(cards) {
  const codes = cards.map(card => card.code);
  if (comboDefs.some(combo => combo.codes.join(',') === codes.join(','))) return false;
  if (comboDefs.length >= 4) return null;
  comboDefs.push({ codes: [...codes], cards: cards.map(card => ({ ...card })) });
  return true;
}
$('nibiru-combo-preset').onclick = () => {
  let added = 0;
  for (const pair of NIBIRU_COMBO_PRESET) {
    const result = addComboCards(pair);
    if (result === null) {
      $('combo-status').textContent = '最多设置 4 个组合。';
      break;
    }
    if (result) added += 1;
  }
  renderComboDefs();
  if (added) $('combo-status').textContent = `已加入 ${added} 个组合，点“查询”查看。`;
};

/* ---------- 条件收集与还原 ---------- */

function phaseValues() {
  return [...document.querySelectorAll('input[name="phase"]:checked')].map((box) => box.value);
}

function collect() {
  const p = new URLSearchParams();
  for (const code of targets.codes()) p.append('code', String(code));
  for (const code of excludes.codes()) p.append('exclude', String(code));
  for (const combo of comboDefs) p.append('combo', combo.codes.join(','));
  const deck = $('deck').value.trim();
  const opdeck = $('opdeck').value.trim();
  if (deck) p.append('deck', JSON.stringify(deck));
  if (opdeck) p.append('opponent', JSON.stringify(opdeck));
  p.append('seat', 'all');
  const phases = phaseValues();
  if (phases.length) { for (const phase of phases) p.append('phase', phase); }
  else p.append('phase_none', '1');
  p.append('metric', $('metric').value);
  p.append('threshold', $('threshold').value);
  p.append('min', $('min').value);
  p.append('sort', $('sort').value);
  p.append('self_range', $('self_range').value);
  p.append('opponent_range', $('opponent_range').value);
  return p;
}

function writeHash(params) {
  try { history.replaceState(null, '', location.pathname + '#' + params.toString()); } catch (_) { /* 忽略 */ }
}

function clearHash() {
  try { history.replaceState(null, '', location.pathname); } catch (_) { /* 忽略 */ }
}

/* ---------- 卡片选择 ---------- */

/* ---------- 卡组候选 ---------- */

function fillDatalist(id, names) {
  const list = $(id);
  list.replaceChildren();
  for (const name of names) {
    if (!name) continue;
    const option = document.createElement('option');
    option.value = name;
    list.append(option);
  }
}

async function loadGroups() {
  if (state.groupsReady === 'loading') return;
  state.groupsReady = 'loading';
  for (const id of ['deck','opdeck']) {$(id).dataset.candidateLoading = '1';delete $(id).dataset.candidateError;}
  try {
    const groups = await SS.api('groups');
    fillDatalist('deck-list', (groups.deck_groups || []).map((x) => x.name).filter(Boolean));
    fillDatalist('opdeck-list', (groups.deck_groups || []).map((x) => x.name).filter(Boolean));
    state.groupsReady = true;
  } catch (_) {
    state.groupsReady = false;   // 可手输；重新聚焦时再试，不永久放弃候选
    for (const id of ['deck','opdeck']) $(id).dataset.candidateError = '卡组候选暂不可用；可手动输入，重新点击输入框重试。';
  } finally {
    for (const id of ['deck','opdeck']) $(id).dataset.candidateLoading = '0';
  }
}
for (const id of ['deck','opdeck']) $(id).addEventListener('focus', () => {
  if (!state.groupsReady) loadGroups();
});

/* ---------- 结果渲染 ---------- */

function percent(value, digits = 1) {
  return value === null || value === undefined || Number.isNaN(value) ? '—' : (value).toFixed(digits) + '%';
}

function summary(data) {
  const f = data.filters || {};
  const parts = [];
  if (f.deck && f.deck.length) parts.push('我方卡组 ' + f.deck.join('、'));
  if (f.opponent && f.opponent.length) parts.push('对手卡组 ' + f.opponent.join('、'));
  const phaseLabels = { main_first: '主卡局先攻', main_second: '主卡局后攻',
                        side_first: 'Side局先攻', side_second: 'Side局后攻' };
  if (f.phase && f.phase.length) parts.push('局面 ' + f.phase.map((x) => phaseLabels[x] || x).join('、'));
  else parts.push('先后攻不限');
  parts.push({ drawn: '抽到后', carried: '投入后', opening: '起手中' }[f.metric] || '');
  if (f.exclude && f.exclude.length) parts.push('起手不含 ' + f.exclude.join('、'));
  const scope = data.rank_scope || {};
  const range = { 100: '前100', 200: '前200', all: '全部' };
  parts.push('我方 ' + (range[scope.self_range] || '—') + ' / 对手 ' + (range[scope.opponent_range] || '—'));
  const self = parts.filter(Boolean).join(' · ');
  const base = data.base || {};
  const line = el('p', '本次结果：' + self, 'summary-line');
  const baseLine = el('p', '基准：' + (base.denominator || 0) + ' 个小局视角，胜率 ' + percent(base.rate) + (base.unknown ? '，胜负未知 ' + base.unknown : ''), 'note');
  return [line, baseLine];
}

function rowCell(text, cls) {
  const cell = el('td', text, cls);
  return cell;
}

function intervalCell(row, baseRate) {
  const cell = el('td', undefined, 'interval-cell');
  const value = row.interval;
  cell.append(el('span', value ? (value[0] * 100).toFixed(1) + '–' + (value[1] * 100).toFixed(1) + '%' : '—', 'interval-text'));
  if (value) {
    const track = el('span', undefined, 'interval-track');
    const band = el('span', undefined, 'interval-band');
    band.style.left = (value[0] * 100).toFixed(2) + '%';
    band.style.width = Math.max(0.5, (value[1] - value[0]) * 100).toFixed(2) + '%';
    if (typeof row.rate === 'number') {
      const dot = el('i', undefined, 'interval-point');
      dot.style.left = row.rate.toFixed(2) + '%';
      track.append(band, dot);
    } else {
      track.append(band);
    }
    if (baseRate !== null && baseRate !== undefined) {
      const base = el('b', undefined, 'interval-base');
      base.style.left = baseRate.toFixed(2) + '%';
      track.append(base);
      cell.title = `95% 区间 ${(value[0] * 100).toFixed(1)}–${(value[1] * 100).toFixed(1)}%；点估计 ${typeof row.rate === 'number' ? row.rate.toFixed(1) : '—'}%；基准 ${baseRate.toFixed(1)}%`;
    }
    cell.append(track);
  }
  return cell;
}

function render(data) {
  const box = $('result');
  box.replaceChildren();
  const panel = el('section', undefined, 'panel');
  panel.append(el('h2', '结果'));
  for (const line of summary(data)) panel.append(line);

  const notes = (data.notes || []).slice();
  if (data.truncated) notes.push('结果已截断，只显示前 ' + data.shown + ' 行。');
  if (notes.length) {
    const ul = el('ul', undefined, 'notes');
    for (const note of notes) ul.append(el('li', note));
    panel.append(ul);
  }

  if (!data.rows || !data.rows.length) {
    panel.append(el('p', '当前条件下没有达到门槛的卡。可以放宽卡组条件、降低样本占比，或减少已选卡。', 'empty'));
    box.append(panel);
    $('export').disabled = true;
    $('status').textContent = '查询完成：0 行。';
    return;
  }

  const wrap = el('div', undefined, 'table-wrap');
  const table = el('table', undefined, 'cards-table');
  const head = el('thead');
  const headRow = el('tr');
  const columns = [
    ['卡名', 'name'], ['卡号', null], ['玩家数', 'people'], ['小局', 'games'],
    ['胜 / 负 / 平', null], ['胜率', 'rate'], ['95% 区间', 'low'], ['样本占比', null],
  ];
  for (const [label, sortKey] of columns) {
    const th = el('th', label);
    if (sortKey === 'people') th.title = '至少有一局满足当前条件的玩家数（去重）';
    if (sortKey) {
      th.classList.add('sortable');
      if (data.filters && data.filters.sort === sortKey) th.setAttribute('aria-sort', 'descending');
      th.onclick = () => { $('sort').value = sortKey; run(); };
    }
    headRow.append(th);
  }
  head.append(headRow);
  table.append(head);

  const baseRate = data.base && typeof data.base.rate === 'number' ? data.base.rate : null;
  const body = el('tbody');
  const scope = data.rank_scope || {};
  for (const row of data.rows) {
    const tr = el('tr');
    const nameCell = rowCell('', 'name-cell');
    if (row.is_combo) {
      tr.classList.add('combo-row');
      const memberNames = (row.combo_members || []).map(card => card.name).join(' + ');
      const title = el('div', row.name + (memberNames ? '：' + memberNames : ''));
      title.title = memberNames;
      const rateText = row.opening_rate === null || row.opening_rate === undefined
        ? '起手率 —' : `起手率 ${row.opening_rate.toFixed(1)}%（${row.opening_hits} / ${row.opening_denominator}）`;
      nameCell.append(title, el('span', rateText + (row.unknown ? `，未知 ${row.unknown}` : ''), 'combo-table-note'));
    } else {
      const link = el('a', row.name);
      const params = new URLSearchParams({
        code: String(row.code), partition: row.partition || 'main',
        first: (data.filters && data.filters.seat) || 'all',
        self_range: scope.self_range || '100', opponent_range: scope.opponent_range || 'all',
      });
      const decks = (data.filters && data.filters.deck) || [];
      if (decks.length === 1) params.set('deck', decks[0]);
      link.href = '/statistics/cards?' + params.toString();
      nameCell.append(link);
    }
    const rateCell = rowCell(percent(row.rate), 'num rate');
    const interval = Array.isArray(row.interval) ? row.interval : null;
    if (baseRate !== null && interval && interval.length === 2) {
      const low = interval[0] * 100, high = interval[1] * 100;
      rateCell.classList.add(low > baseRate ? 'higher' : high < baseRate ? 'lower' : 'uncertain');
    } else {
      rateCell.classList.add('uncertain');
    }
    tr.append(nameCell, rowCell(row.is_combo ? '组合' : '#' + row.code, 'code-cell'),
      rowCell(String(row.people), 'num'), rowCell(String(row.games), 'num'),
      rowCell(row.wins + ' / ' + row.losses + ' / ' + row.draws, 'num'),
      rateCell,
      intervalCell(row, baseRate),
      rowCell(row.sample_percentage === null ? '—' : row.sample_percentage.toFixed(2) + '%', 'num'));
    body.append(tr);
  }
  table.append(body);
  wrap.append(table);
  panel.append(wrap);
  panel.append(el('p', '区间为 Wilson 95% 置信区间；样本占比 = 该卡小局数 / 本次基准小局数。胜率颜色：绿 = 区间整体高于基准，红 = 区间整体低于基准，黑 = 区间跨基准或无数据；区间条中竖线为胜率，灰线为基准。', 'note'));
  panel.append(el('p', '组合行：起手率 = 同时抓到组合 / 有完整初始手牌记录的视角；胜率 = 组合起手后的小局胜率。', 'note'));
  box.append(panel);
  $('export').disabled = false;
  $('status').textContent = '查询完成：' + data.rows.length + ' 行（共 ' + data.total + ' 行达到门槛）。';
}

function fail(error) {
  const box = $('result');
  box.replaceChildren();
  const panel = el('section', undefined, 'panel error');
  panel.setAttribute('role', 'alert');
  panel.append(el('h2', '查询失败'), el('p', String(error && error.message || error)));
  box.append(panel);
  $('export').disabled = true;
  $('status').textContent = '查询失败。';
}

/* ---------- 查询 ---------- */

function setBusy(busy) {
  $('run').disabled = busy;
  $('run').textContent = busy ? '查询中…' : '查询';
  $('export').disabled = busy || !state.result || !state.result.rows || !state.result.rows.length;
}

function waitForRetry(ms, signal) {
  return new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', abort);
      reject(new DOMException('查询已取消', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', abort);
      resolve();
    }, ms);
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
  });
}

async function fetchReport(url, signal, onRetry) {
  for (let attempt = 0; ; attempt += 1) {
    signal.throwIfAborted();
    const response = await fetch(url,
      { cache: 'no-store', credentials: 'same-origin', signal });
    if (response.ok) return response.json();
    const text = await response.text();
    let payload;
    try { payload = JSON.parse(text); } catch (_) { /* 兼容旧版纯文本错误 */ }
    let message = payload && payload.error;
    if (!message && (response.headers.get('Content-Type') || '').startsWith('text/plain')) {
      message = text.trim().slice(0, 200);
    }
    if ([429, 502, 503, 504].includes(response.status) && attempt < 3) {
      const retryAfter = response.headers.get('Retry-After');
      const seconds = retryAfter && !Number.isNaN(Number(retryAfter)) ? Number(retryAfter)
        : retryAfter && Number.isFinite(Date.parse(retryAfter)) ? (Date.parse(retryAfter) - Date.now()) / 1000
        : Number(payload && payload.retry_after) || 2 ** attempt;
      const delay = Math.min(10, Math.max(1, seconds));
      signal.throwIfAborted();
      onRetry(delay, attempt + 1);
      await waitForRetry(delay * 1000, signal);
      continue;
    }
    throw Error(message || '查询失败（' + response.status + '），请稍后重试。');
  }
}

async function run(options) {
  const push = !options || options.push !== false;
  const version = ++state.request;
  if (state.controller) state.controller.abort();
  state.controller = new AbortController();
  const signal = state.controller.signal;
  targets.close();
  excludes.close();
  setBusy(true);
  $('status').textContent = '查询中…';
  $('result').replaceChildren();
  try {
    const params = collect();
    const data = await fetchReport('/statistics/api/card-report?' + params.toString(), signal,
      (seconds, attempt) => {
        if (version === state.request) $('status').textContent =
          '服务繁忙，' + seconds + ' 秒后自动重试（' + attempt + '/3）…';
      });
    if (version !== state.request) return;        // 旧响应不覆盖新查询
    state.result = data;
    render(data);
    if (push) writeHash(params);
  } catch (error) {
    if (error && error.name === 'AbortError') return;
    if (version !== state.request) return;
    state.result = null;
    fail(error);
  } finally {
    if (version === state.request) setBusy(false);
  }
}

/* ---------- 导出 / 重置 / 还原 ---------- */

function exportCsv() {
  const data = state.result;
  if (!data || !data.rows || !data.rows.length) return;
  const quote = (value) => '"' + String(value).replace(/"/g, '""') + '"';
  const lines = ['code,name,games,wins,losses,draws,rate,ci_low,ci_high,sample_percentage'];
  for (const row of data.rows) {
    lines.push([
      row.code, quote(row.name), row.games, row.wins, row.losses, row.draws,
      (row.rate / 100).toFixed(4),
      row.interval ? row.interval[0].toFixed(4) : '',
      row.interval ? row.interval[1].toFixed(4) : '',
      row.sample_percentage === null ? '' : row.sample_percentage.toFixed(2),
    ].join(','));
  }
  const blob = new Blob(['\ufeff' + lines.join('\n') + '\n'], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'card-report.csv';
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function resetFields() {
  state.result = null;
  targets.reset();
  excludes.reset();
  $('deck').value = '';
  $('opdeck').value = '';
  $('metric').value = 'opening';
  $('self_range').value = '200';
  $('opponent_range').value = '200';
  $('threshold').value = '1';
  $('min').value = '0';
  $('sort').value = 'rate';
  for (const box of document.querySelectorAll('input[name="phase"]')) box.checked = true;
  for (const button of document.querySelectorAll('.tag[aria-pressed="true"]')) button.setAttribute('aria-pressed', 'false');
}

function reset() {
  state.request++;                       // 使在途请求失效
  if (state.controller) state.controller.abort();
  resetFields();
  $('result').replaceChildren();
  $('export').disabled = true;
  $('status').textContent = '已重置。';
  clearHash();
  setBusy(false);
}

async function restore() {
  if (!location.hash || location.hash.length < 2) return;
  resetFields();                         // 先归零，避免 hash 里没写的条件沿用上一次的值
  const params = new URLSearchParams(location.hash.slice(1));
  for (const [key, picker] of [['code', targets], ['exclude', excludes]]) {
    for (const raw of params.getAll(key)) {
      const value = Number(raw);
      if (Number.isInteger(value) && value > 0) picker.add({ code: value, name: '卡号 ' + value });
    }
  }
  const deck = params.getAll('deck').map((v) => { try { return JSON.parse(v); } catch (_) { return v; } })[0];
  const opdeck = params.getAll('opponent').map((v) => { try { return JSON.parse(v); } catch (_) { return v; } })[0];
  if (deck) $('deck').value = deck;
  if (opdeck) $('opdeck').value = opdeck;
  const seat = params.get('seat');
  const phaseBox = (value) => [...document.querySelectorAll('input[name="phase"]')].find((box) => box.value === value);
  const phases = params.getAll('phase');
  if (phases.length) {
    for (const box of document.querySelectorAll('input[name="phase"]')) box.checked = false;
    for (const value of phases) { const box = phaseBox(value); if (box) box.checked = true; }
  } else if (seat === 'first' || seat === 'second') {
    // 旧链接：seat=first/second 等价于主卡局与 Side局的同一先后攻都选中。
    for (const box of document.querySelectorAll('input[name="phase"]')) box.checked = false;
    for (const value of ['main_' + seat, 'side_' + seat]) { const box = phaseBox(value); if (box) box.checked = true; }
  }
  if (params.get('metric')) $('metric').value = params.get('metric');
  if (params.get('self_range')) $('self_range').value = params.get('self_range');
  if (params.get('opponent_range')) $('opponent_range').value = params.get('opponent_range');
  if (params.get('threshold') !== null) $('threshold').value = params.get('threshold');
  if (params.get('min') !== null) $('min').value = params.get('min');
  if (params.get('sort')) $('sort').value = params.get('sort');
  await run({ push: false });
  // 用响应里的卡名补全标签，避免一直显示"卡号 xxx"。
  if (state.result && state.result.rows) {
    const names = new Map(state.result.rows.map((row) => [row.code, row.name]));
    let changed = false;
    for (const [code, name] of targets.cards) {
      if (names.has(code) && name !== names.get(code)) {
        targets.cards.set(code, names.get(code));
        changed = true;
      }
    }
    if (changed) targets.render();
  }
}

/* ---------- 绑定 ---------- */

$('form').addEventListener('submit', (event) => { event.preventDefault(); run(); });
$('reset').addEventListener('click', reset);
$('export').addEventListener('click', exportCsv);
window.addEventListener('hashchange', () => {
  if (!location.hash) return;
  restore();
});
$('phaseAll').onclick = () => { for (const box of document.querySelectorAll('input[name="phase"]')) box.checked = true; $('status').textContent = '已全选局面，点查询生效。'; };
$('phaseNone').onclick = () => { for (const box of document.querySelectorAll('input[name="phase"]')) box.checked = false; $('status').textContent = '已取消全部局面（查询将没有结果），点查询生效。'; };
for (const id of ['deck', 'opdeck', 'metric', 'self_range', 'opponent_range', 'threshold', 'min', 'sort']) {
  $(id).addEventListener('change', () => { $('status').textContent = '条件已改，点查询生效。'; });
}

(async () => {
  restore();
  loadGroups();
})();
})();
