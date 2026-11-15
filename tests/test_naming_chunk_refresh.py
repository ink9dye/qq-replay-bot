import json
import time
import unittest

import test_watch_replay_evidence as evidence_tests
from bot.series_naming import (apply_names_chunk, drop_queue, load_config, pending_series_builds,
                               queued_builds, rename_builds, rename_targets)


class NamingChunkRefreshTests(unittest.TestCase):
    """改名/改阈值后按批重算：不再因为待重算多就整体跳过。"""

    setUp = evidence_tests.ReplayEvidenceTests.setUp
    upload_replay = evidence_tests.ReplayEvidenceTests.upload_replay

    def configure(self, tag='测试系列'):
        (self.root/'card-series-reference.json').write_text(json.dumps(dict(
            version=1, semantics='contains_official_series_in_observed_main_or_extra',
            cards={str(c): dict(tags=[tag], complete=True) for c in [1, 2, 3, 8, 9]})), encoding='utf8')
        self.set_group(tag)

    def set_group(self, name):
        """模拟网页「组合编辑」：把官方系列挂到一个自定义组名下（改名只动这个文件）。"""
        (self.root/'series-naming.json').write_text(json.dumps(dict(
            primary_threshold=1, secondary_threshold=1, min_main_copies=1,
            groups=[dict(name=name, sources=['测试系列'])]), ensure_ascii=False), encoding='utf8')

    def labels(self):
        return [r[0] for r in self.store._db.execute(
            'SELECT deck_type FROM stat_builds ORDER BY fingerprint')]

    def forget_names(self):
        with self.store._db:
            self.store._db.execute("UPDATE stat_builds SET deck_type=NULL,classification_version=NULL,"
                                   "classification_source='unclassified',classification_status='unknown'")

    def revision(self):
        return self.store._db.execute('SELECT revision FROM stat_classification_state').fetchone()[0]

    def test_chunk_names_only_limit_builds_and_reports_remaining(self):
        self.configure()
        self.upload_replay()
        self.forget_names()
        config = load_config(self.root)
        now = time.time()
        self.assertEqual(len(pending_series_builds(self.store._db, now, config, replay_root=None)), 2)

        first = apply_names_chunk(self.store._db, now, config, limit=1)
        self.assertEqual((first['total'], first['pending'], first['changed']), (2, 1, 1))
        self.assertEqual(sum(1 for name in self.labels() if name), 1)

        second = apply_names_chunk(self.store._db, now, config, limit=1)
        self.assertEqual((second['total'], second['pending'], second['changed']), (1, 0, 1))
        self.assertEqual(self.labels(), ['测试系列', '测试系列'])
        self.assertGreater(self.revision(), 0)

    def test_rename_reaches_existing_builds_after_config_change(self):
        self.configure()
        self.set_group('旧名')
        self.upload_replay()
        self.assertEqual(self.labels(), ['旧名', '旧名'])

        # 组合改名：规则文件变化 → 版本串变化 → 旧构筑重新排队，按批写法即可生效。
        self.set_group('新名')
        config = load_config(self.root)
        now = time.time()
        pending = pending_series_builds(self.store._db, now, config, replay_root=None)
        self.assertEqual(len(pending), 2)

        result = apply_names_chunk(self.store._db, now, config, limit=500)
        self.assertEqual((result['pending'], result['changed']), (0, 2))
        self.assertEqual(self.labels(), ['新名', '新名'])

    def test_chunk_with_nothing_pending_is_noop(self):
        self.configure()
        self.upload_replay()
        config = load_config(self.root)
        result = apply_names_chunk(self.store._db, time.time(), config, limit=500)
        self.assertEqual((result['pending'], result['total'], result['changed']), (0, 0, 0))

    def test_rename_only_touches_builds_carrying_that_field(self):
        """改名只重算名字里含该字段的构筑，别的构筑一条都不碰。"""
        self.configure()
        self.set_group('旧名')
        self.upload_replay()
        self.assertEqual(self.labels(), ['旧名', '旧名'])
        before_other = self.store._db.execute(
            "SELECT count(*) FROM stat_builds WHERE deck_type IS NOT '旧名'").fetchone()[0]

        self.set_group('新名')
        config = load_config(self.root)
        targets = rename_targets(self.store._db, '旧名', '新名', config)
        self.assertEqual(len(targets), 2)

        result = rename_builds(self.store._db, '旧名', '新名', config, batch=1)
        self.assertEqual((result['total'], result['changed']), (2, 2))
        self.assertEqual(self.labels(), ['新名', '新名'])
        # 只改了这 2 副：其它构筑的分类版本没被顺手刷新。
        self.assertEqual(self.store._db.execute(
            "SELECT count(*) FROM stat_builds WHERE deck_type IS NOT '新名'").fetchone()[0],
            before_other)

    def test_rename_skips_builds_already_using_the_new_name(self):
        """把「地机」改成「地机ftk」时，已经是「地机ftk」的构筑不重复处理。"""
        self.configure()
        self.set_group('地机')
        self.upload_replay()
        self.set_group('地机ftk')
        config = load_config(self.root)
        rename_builds(self.store._db, '地机', '地机ftk', config)
        self.assertEqual(self.labels(), ['地机ftk', '地机ftk'])

        # 再跑一次：全都带新名，目标集合为空。
        self.assertEqual(rename_targets(self.store._db, '地机', '地机ftk', config), [])

    def test_rename_defers_builds_the_pages_are_not_showing(self):
        """前端页面看不到的构筑不立刻改，写进慢速队列，由后台按批追。"""
        self.configure()
        self.set_group('旧名')
        self.upload_replay()
        self.assertEqual(self.labels(), ['旧名', '旧名'])
        self.set_group('新名')
        config = load_config(self.root)

        # 假设这两副都不在页面上（名册/饼图/近三天对局都没它）：
        result = rename_builds(self.store._db, '旧名', '新名', config, visible=set())
        self.assertEqual((result['total'], result['front'], result['changed']), (2, 0, 0))
        self.assertEqual(result['queued'], 2)
        self.assertEqual(self.labels(), ['旧名', '旧名'])

        # 慢速队列被后台读到后按批重算，名字才会变。
        queued = queued_builds(self.store._db, limit=50)
        self.assertEqual(len(queued), 2)
        apply_names_chunk(self.store._db, time.time(), config, builds=queued, limit=50)
        self.assertEqual(sorted(self.labels()), ['新名', '新名'])
        drop_queue(self.store._db, queued)
        self.assertEqual(queued_builds(self.store._db, limit=50), [])

    def test_rename_immediately_fixes_builds_visible_on_pages(self):
        """页面正在展示的那几副必须立刻改；同一次改名里其余构筑进队列。"""
        self.configure()
        self.set_group('旧名')
        self.upload_replay()
        self.set_group('新名')
        config = load_config(self.root)
        fingerprints = [row[0] for row in self.store._db.execute(
            'SELECT fingerprint FROM stat_builds ORDER BY fingerprint')]

        result = rename_builds(self.store._db, '旧名', '新名', config,
                               visible={fingerprints[0]})
        self.assertEqual((result['front'], result['changed'], result['queued']), (1, 1, 1))
        names = dict(self.store._db.execute('SELECT fingerprint,deck_type FROM stat_builds'))
        self.assertEqual(names[fingerprints[0]], '新名')
        self.assertEqual(names[fingerprints[1]], '旧名')
        self.assertEqual(queued_builds(self.store._db, limit=10), [fingerprints[1]])


if __name__ == '__main__':
    unittest.main()
