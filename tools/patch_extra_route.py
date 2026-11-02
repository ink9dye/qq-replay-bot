# -*- coding: utf-8 -*-
"""W77：额外卡统计后端路由。"""
import pathlib

p = pathlib.Path(r'D:\pythons\qq-replay-bot\bot\statistics_portal.py')
t = p.read_text(encoding='utf-8')
old = """        if parts == ['statistics', 'cards-report']:
            return self._reply(200, Path(__file__).with_name('statistics_cards_report.html').read_text(encoding='utf8'), 'text/html; charset=utf-8')"""
new = """        if parts == ['statistics', 'cards-report']:
            return self._reply(200, Path(__file__).with_name('statistics_cards_report.html').read_text(encoding='utf8'), 'text/html; charset=utf-8')
        if parts == ['statistics', 'cards-extra']:
            return self._reply(200, Path(__file__).with_name('statistics_cards_extra.html').read_text(encoding='utf8'), 'text/html; charset=utf-8')
        if parts == ['statistics', 'api', 'cards-extra']:
            from .statistics_extra_report import query as extra_report
            from .statistics_rank_scope import parse_ranges, scoped_query
            args = parse_qs(parsed.query)
            try:
                ranges = parse_ranges(args)
                seat = args.get('seat', ['all'])[0]
                if seat not in ('all', 'first', 'second'):
                    raise ValueError()
                internal = args.get('internal', ['0'])[0] == '1'
                phases = args.get('phase', [])
                phase_none = args.get('phase_none', ['0'])[0] == '1'
            except (ValueError, TypeError):
                return self._reply(400, json.dumps({'error': '额外卡筛选条件无效。'}, ensure_ascii=False),
                                   'application/json; charset=utf-8')
            from .statistics_card_report import Filters
            filters = Filters((), (), (), (), None if seat == 'all' else seat == 'first', internal)
            try:
                return self._query_reply(lambda db: scoped_query(
                    ranges, lambda: extra_report(db, self.replay_root, float(self.clock()), filters=filters,
                                                 phases=phases, phase_none=phase_none)))
            except ValueError as exc:
                return self._reply(400, json.dumps({'error': str(exc)}, ensure_ascii=False),
                                   'application/json; charset=utf-8')"""
assert t.count(old) == 1
t = t.replace(old, new, 1)
p.write_text(t, encoding='utf-8', newline='')
print('patched')
