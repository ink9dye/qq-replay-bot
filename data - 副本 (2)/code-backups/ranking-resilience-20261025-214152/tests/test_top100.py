import copy
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock

from bot.store import Store
from bot.top100 import parse
from retention_policy import RETENTION_SECONDS
import test_statistics_facts as F
from test_bot import temporary_service

NOW=F.NOW


def roster(names=()):
    names=list(names)+['填充玩家'+str(i) for i in range(100-len(names))]
    return [dict(username=n,pt=2000-i,id=None) for i,n in enumerate(names)]


class Top100Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.replays=self.root/'replays';self.replays.mkdir()
        self.store=Store(self.root/'db.sqlite3');self.addCleanup(self.store.close)
        self.row,self.path=F.StatisticsFactsTests.source(self)
        self.store.import_replay_facts(self.row,self.path,None,NOW)

    def eligible(self,internal=False,now=NOW):
        return self.store.current_top100_views(self.replays,now,internal)

    def test_dropout_reentry_and_internal_match_use_current_membership(self):
        self.store.replace_top100(roster(['甲','乙']),NOW)
        self.assertEqual(len(self.eligible()['views']),2)
        self.assertEqual(len(self.eligible(True)['views']),2)
        expires=self.store.fact_games(self.replays,NOW)[0]['expires_at']
        self.store.replace_top100(roster(['甲']),NOW+1)
        self.assertEqual([v['player'] for v in self.eligible()['views']],['甲'])
        self.assertEqual(self.eligible(True)['views'],[])
        self.assertEqual(len(self.store.fact_details(self.row['digest'],self.replays,NOW)['views']),2)
        self.store.replace_top100(roster(['甲','乙']),NOW+2)
        self.assertEqual(len(self.eligible(True)['views']),2)
        self.assertEqual(self.store.fact_games(self.replays,NOW)[0]['expires_at'],expires)

    def test_failed_or_partial_refresh_keeps_last_success_and_flags_stale(self):
        state=self.store.replace_top100(roster(['甲','乙']),NOW)
        with self.assertRaises(ValueError):self.store.replace_top100(roster()[:-1],NOW+10)
        self.store.top100_failed(NOW+10,'refresh_failed')
        current=self.eligible()
        self.assertEqual(current['roster']['version'],state['version'])
        self.assertEqual(current['roster']['updated_at'],NOW)
        self.assertTrue(current['roster']['stale'])
        self.assertEqual(len(current['views']),2)
        self.assertEqual(len(self.store.top100_snapshot(NOW+601)['members']),100)

    def test_initial_no_roster_never_means_every_player_is_top100(self):
        self.assertFalse(self.eligible()['roster']['available'])
        self.assertEqual(self.eligible()['views'],[])

    def test_expired_or_missing_source_cannot_return_after_reentry(self):
        self.store.replace_top100(roster(['甲','乙']),NOW)
        self.assertEqual(self.eligible(now=NOW+RETENTION_SECONDS)['views'],[])
        self.path.unlink()
        self.assertEqual(self.eligible()['views'],[])

    def test_duplicate_nonfinite_and_unsorted_rosters_are_rejected_without_partial_write(self):
        self.store.replace_top100(roster(['甲']),NOW)
        bad=[]
        duplicate=roster();duplicate[-1]['username']=duplicate[0]['username'];bad.append(duplicate)
        nan=roster();nan[0]['pt']=float('nan');bad.append(nan)
        unsorted=roster();unsorted[2]['pt']=99999;bad.append(unsorted)
        empty=roster();empty[2]['username']='';bad.append(empty)
        boolean=roster();boolean[2]['pt']=True;bad.append(boolean)
        for data in bad:
            with self.subTest(data=data[:1]):
                with self.assertRaises(ValueError):self.store.replace_top100(data,NOW+1)
                self.assertEqual(self.store.top100_snapshot(NOW)['version'],1)
                self.assertEqual(self.eligible()['views'][0]['player'],'甲')

    def test_query_keeps_one_roster_version_during_concurrent_successful_refresh(self):
        import bot.top100 as module
        self.store.replace_top100(roster(['甲']),NOW)
        other=Store(self.root/'db.sqlite3');self.addCleanup(other.close)
        original=module.snapshot;called=False
        def switch(db,now):
            nonlocal called
            state=original(db,now)
            if not called:
                called=True
                other.replace_top100(roster(['乙']),NOW+1)
            return state
        with patch.object(module,'snapshot',switch):
            result=self.eligible()
        self.assertEqual(result['roster']['version'],1)
        self.assertEqual([v['player'] for v in result['views']],['甲'])
        self.assertEqual([v['player'] for v in self.eligible()['views']],['乙'])

    def test_exclusion_scope_is_separate_from_membership_and_can_restore(self):
        self.store.replace_top100(roster(['甲','乙']),NOW)
        self.store._db.execute('INSERT INTO stat_exclusions VALUES(?,?,?,?)',(self.row['digest'],'甲','default','手动排除'))
        self.store._db.commit()
        self.assertEqual([v['player'] for v in self.eligible()['views']],['乙'])
        self.store._db.execute('DELETE FROM stat_exclusions');self.store._db.commit()
        self.assertEqual(len(self.eligible()['views']),2)

    def test_official_position_order_and_optional_official_ids(self):
        data=roster(['甲','乙']);data[0]['id']=1234
        members=parse(data)
        self.assertEqual((members[0]['player'],members[0]['rank'],members[-1]['rank']),('甲',1,100))
        self.assertEqual(members[0]['official_id'],'1234')
        self.assertIsNone(members[1]['official_id'])


class Top100UnifiedRulesTests(unittest.TestCase):
    def test_current_roster_merges_into_same_loop_without_subscribing_anyone(self):
        with temporary_service() as service:
            service.config['_pycharm_watch_entry']=True;service._top100_enabled=True
            service.store.upsert_subscription('10001','甲',None,False,service.clock())
            service.store.upsert_subscription('10001','名单外',None,False,service.clock())
            service.store.replace_top100(roster(['甲','乙']),service.clock())
            names,rules=service.current_rules()
            self.assertEqual(len(names),101)
            self.assertEqual(names.count('甲'),1)
            self.assertIn('乙',names);self.assertIn('名单外',names)
            self.assertEqual(len(service.store.list_subscriptions()),2)
            self.assertEqual(rules,[])


class Top100RefreshLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_attempt_per_interval_and_failure_retains_successful_roster(self):
        with temporary_service() as service:
            with patch('bot.top100.fetch',return_value=roster(['甲'])), \
                    patch('bot.service.asyncio.sleep',new=AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):await service.top100_loop()
            first=service.store.top100_snapshot(service.clock())
            self.assertEqual(first['version'],1)
            with patch('bot.top100.fetch',side_effect=OSError('offline')) as fetch, \
                    patch('bot.service.asyncio.sleep',new=AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):await service.top100_loop()
                fetch.assert_called_once()
            failed=service.store.top100_snapshot(service.clock())
            self.assertEqual(failed['members'],first['members'])
            self.assertEqual(failed['version'],first['version'])
            self.assertTrue(failed['stale'])
