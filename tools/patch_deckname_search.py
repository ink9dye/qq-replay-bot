# -*- coding: utf-8 -*-
"""W81：卡组命名页支持按卡组名包含搜索玩家。"""
import pathlib

p = pathlib.Path(r'D:\pythons\qq-replay-bot\bot\statistics_deck_naming.py')
t = p.read_text(encoding='utf-8')
old = "def list_rows(db, now, scope='100', query='', replay_root=None):\n    \"\"\"scope=100/200/all/unnamed；query 非空时跨全部已知玩家搜索（含榜外已采集）。\"\"\""
new = "def list_rows(db, now, scope='100', query='', replay_root=None, deck_query=''):\n    \"\"\"scope=100/200/all/unnamed；query/玩家名、deck_query/卡组名非空时跨全部已知玩家搜索。\"\"\""
assert t.count(old) == 1
t = t.replace(old, new, 1)
old = """    query = (query or '').strip()
    if len(query) > 128:
        raise ValueError('搜索词过长。')
    roster = _roster(db)"""
new = """    query = (query or '').strip()
    deck_query = (deck_query or '').strip()
    if len(query) > 128 or len(deck_query) > 128:
        raise ValueError('搜索词过长。')
    roster = _roster(db)"""
assert t.count(old) == 1
t = t.replace(old, new, 1)
old = """    names = set(roster) | set(views)
    searched = bool(query)
    if searched:
        lowered = query.lower()
        names = {name for name in names if lowered in name.lower()}
    elif scope == '100':"""
new = """    names = set(roster) | set(views)
    searched = bool(query or deck_query)
    if deck_query:
        lowered_deck = deck_query.lower()
        names = {name for name in names
                 if lowered_deck in ((views.get(name) or {}).get('deck_type') or '').lower()}
        if query:
            lowered = query.lower()
            names = {name for name in names if lowered in name.lower()}
    elif query:
        lowered = query.lower()
        names = {name for name in names if lowered in name.lower()}
    elif scope == '100':"""
assert t.count(old) == 1
t = t.replace(old, new, 1)
old = """    return dict(version=2, scope='search' if searched else scope, query=query, total=total,"""
new = """    return dict(version=2, scope='search' if searched else scope, query=query, deck_query=deck_query, total=total,"""
assert t.count(old) == 1
t = t.replace(old, new, 1)
p.write_text(t, encoding='utf-8', newline='')

p = pathlib.Path(r'D:\pythons\qq-replay-bot\bot\statistics_portal.py')
t = p.read_text(encoding='utf-8')
old = "scope=args.get('scope',['100'])[0];query=args.get('q',[''])[0]"
new = "scope=args.get('scope',['100'])[0];query=args.get('q',[''])[0];deck_query=args.get('deck',[''])[0]"
assert t.count(old) == 1
t = t.replace(old, new, 1)
old = "list_rows(db,float(self.clock()),scope,query,self.replay_root)"
new = "list_rows(db,float(self.clock()),scope,query,self.replay_root,deck_query=deck_query)"
assert t.count(old) == 1
t = t.replace(old, new, 1)
p.write_text(t, encoding='utf-8', newline='')
print('patched')
