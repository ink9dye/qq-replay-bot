# -*- coding: utf-8 -*-
"""W80：额外卡统计增加排序选择，模仿卡片统计。"""
import pathlib

p = pathlib.Path(r'D:\pythons\qq-replay-bot\bot\statistics_extra_report.py')
t = p.read_text(encoding='utf-8')
old = "MAX_ROWS = 500\n\n\ndef query(db, replay_root, now, filters=None, phases=(), phase_none=False, limit=MAX_ROWS):"
new = "MAX_ROWS = 500\nSORTS = ('carry', 'players', 'appearance', 'win', 'code')\n\n\ndef query(db, replay_root, now, filters=None, phases=(), phase_none=False, limit=MAX_ROWS, sort='carry'):"
assert t.count(old) == 1
t = t.replace(old, new, 1)
old = """    rows.sort(key=lambda row: (-row['carry_games'], -(row['appearance_rate'] or -1), row['code']))
    total = len(rows)"""
new = """    if sort == 'players':
        rows.sort(key=lambda row: (-row['carry_players'], -(row['carry_rate'] or -1), row['code']))
    elif sort == 'appearance':
        rows.sort(key=lambda row: (-(row['appearance_rate'] if row['appearance_rate'] is not None else -1), -row['carry_games'], row['code']))
    elif sort == 'win':
        rows.sort(key=lambda row: (-(row['win_rate'] if row['win_rate'] is not None else -1), -row['carry_games'], row['code']))
    elif sort == 'code':
        rows.sort(key=lambda row: row['code'])
    else:
        rows.sort(key=lambda row: (-(row['carry_rate'] if row['carry_rate'] is not None else -1), -row['carry_games'], row['code']))
    total = len(rows)"""
assert t.count(old) == 1
t = t.replace(old, new, 1)
old = "            opponent_series=list(filters.opponent_series) if filters else [],\n            phase=list(phases),"
new = "            opponent_series=list(filters.opponent_series) if filters else [],\n            phase=list(phases),\n            sort=sort,"
assert t.count(old) == 1
t = t.replace(old, new, 1)
p.write_text(t, encoding='utf-8', newline='')

p = pathlib.Path(r'D:\pythons\qq-replay-bot\bot\statistics_portal.py')
t = p.read_text(encoding='utf-8')
old = "                phase_none=args.get('phase_none',['0'])[0]=='1'\n                decks=tuple"
new = "                phase_none=args.get('phase_none',['0'])[0]=='1'\n                sort=args.get('sort',['carry'])[0]\n                if sort not in ('carry','players','appearance','win','code'):raise ValueError()\n                decks=tuple"
assert t.count(old) == 1
t = t.replace(old, new, 1)
old = "db,self.replay_root,float(self.clock()),filters=filters,phases=phases,phase_none=phase_none)))"
new = "db,self.replay_root,float(self.clock()),filters=filters,phases=phases,phase_none=phase_none,sort=sort)))"
assert t.count(old) == 1
t = t.replace(old, new, 1)
p.write_text(t, encoding='utf-8', newline='')
print('patched')
