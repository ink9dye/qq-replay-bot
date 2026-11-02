# -*- coding: utf-8 -*-
"""W85：移除所有页面的“仅双方都在前100/前100内战”控件。"""
import pathlib

BOT = pathlib.Path(r'D:\pythons\qq-replay-bot\bot')


def edit(name, pairs):
    p = BOT / name
    t = p.read_text(encoding='utf-8')
    for old, new in pairs:
        assert t.count(old) == 1, (name, old[:60], t.count(old))
        t = t.replace(old, new, 1)
    p.write_text(t, encoding='utf-8', newline='')
    print('patched', name)


edit('statistics_cards_report.html', [
    ('<label class="check"><input id="internal" type="checkbox"> 仅双方都在前100（前100内战）</label>\n', ''),
])
edit('statistics_cards_report.js', [
    ("  if ($('internal').checked) p.append('internal', '1');\n", ''),
    ('  $(\'internal\').checked = false;\n', ''),
    ("  $('internal').checked = params.get('internal') === '1';\n", ''),
    ("'sort', 'internal']", "'sort']"),
])
edit('statistics_cards_extra.html', [
    ('<label style="flex-direction:row;align-items:center;gap:6px"><input id="internal" type="checkbox" style="width:auto"> 仅双方当前均在前100</label>\n', ''),
    ("internal:$('internal').checked?'1':'0',sort:$('sort').value", "sort:$('sort').value"),
])
edit('statistics_card_metrics.html', [
    ('<label><input id="internal" type="checkbox"> 仅双方当前均在前100（仅小局）</label>', ''),
])
edit('statistics_card_metrics.js', [
    ("first:$('first').value,internal:$('internal').checked?'1':'0',basis:", "first:$('first').value,basis:"),
    ("['partition','deck','opponent','first','internal','investbasis']", "['partition','deck','opponent','first','investbasis']"),
    ("$('selfRange').onchange=$('opponentRange').onchange=()=>{$('internal').checked=false;load()};$('internal').onchange=()=>{if($('internal').checked){$('selfRange').value='100';$('opponentRange').value='100'}load()};",
     "$('selfRange').onchange=$('opponentRange').onchange=load;"),
])
edit('statistics_matrix.html', [
    ('<label><input id="internal" type="checkbox" style="display:inline;width:auto"> 仅双方当前均在前100</label>', ''),
    ('默认前200内战（我方/对手范围可调）', '默认前200/前200（我方/对手范围可调）'),
    ("const id=++cellRequest,inner=$('internal').checked,args=new URLSearchParams({row:JSON.stringify(current.row),column:JSON.stringify(current.column),first:$('first').value,internal:inner?'1':'0',threshold:$('cellThreshold').value.trim()});if(!inner){args.set('self_range',$('selfRange').value);args.set('opponent_range',$('opponentRange').value)}",
     "const id=++cellRequest,args=new URLSearchParams({row:JSON.stringify(current.row),column:JSON.stringify(current.column),first:$('first').value,threshold:$('cellThreshold').value.trim()});args.set('self_range',$('selfRange').value);args.set('opponent_range',$('opponentRange').value);"),
    ("const id=++request,inner=$('internal').checked,args=new URLSearchParams({first:$('first').value,internal:inner?'1':'0'});if(!inner){args.set('self_range',$('selfRange').value);args.set('opponent_range',$('opponentRange').value)}",
     "const id=++request,args=new URLSearchParams({first:$('first').value});args.set('self_range',$('selfRange').value);args.set('opponent_range',$('opponentRange').value);"),
    ("$('refresh').onclick=load;$('first').onchange=load;$('internal').onchange=load;", "$('refresh').onclick=load;$('first').onchange=load;"),
])
