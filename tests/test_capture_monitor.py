import tempfile
import unittest
import json
import os
from pathlib import Path
from types import SimpleNamespace

import watch


def room(key,started=True):return watch.RoomInfo(key,key,key,['甲','乙'],started)


class CaptureMonitorTests(unittest.TestCase):
    def test_status_writer_binds_process_and_preserves_observation_time(self):
        from bot.service import BotService
        with tempfile.TemporaryDirectory() as tmp:
            service = SimpleNamespace(clock=lambda: 100, _capture_status_written=0,
                _top100_enabled=True, data_dir=Path(tmp),
                store=SimpleNamespace(top100_snapshot=lambda now: dict(version=1, updated_at=90,
                    stale=False, last_error=None, consecutive_failures=0, members=[])))
            BotService.on_capture_status(service, dict(updated_at=99, active_observations=2, watch_pid=-1))
            saved = json.loads((Path(tmp)/'capture-status.json').read_text(encoding='utf8'))
            self.assertEqual(saved['watch_pid'], os.getpid())
            self.assertEqual(saved['updated_at'], 99)
            self.assertEqual(saved['active_observations'], 2)
            self.assertFalse((Path(tmp)/'capture-status.tmp').exists())

    def test_oldest_first_queue_limit_dedup_and_missed_room_counter(self):
        monitor=watch.CaptureMonitor(1)
        old,new=room('old'),room('new')
        self.assertEqual([r.key for r in monitor.candidates([old],[old],{}, {},1)],['old'])
        chosen=monitor.candidates([new,old],[new,old,old],{}, {},2)
        self.assertEqual([r.key for r in chosen],['old'])
        self.assertEqual(monitor.counts['discovered_rooms'],2)
        self.assertEqual(monitor.overflow,1)
        monitor.joined('old')
        self.assertEqual(monitor.waiting,1)
        monitor.candidates([old],[old],{'old':object()}, {},3)
        self.assertEqual(monitor.counts['disappeared_without_join'],1)
        monitor.candidates([],[],{}, {},4)
        self.assertEqual(monitor.counts['disappeared_without_join'],1)
        self.assertEqual(monitor.rooms,{})

    def test_waiting_room_never_started_and_cooldown_are_not_false_missed_games(self):
        monitor=watch.CaptureMonitor(2)
        waiting=room('waiting',False)
        monitor.candidates([waiting],[waiting],{}, {'waiting':20},10)
        self.assertEqual((monitor.waiting,monitor.cooling),(0,1))
        monitor.candidates([],[],{}, {},21)
        self.assertEqual(monitor.counts['disappeared_without_join'],0)
        monitor.finished('saved');monitor.finished('config')
        self.assertEqual(monitor.counts['completed_observations'],1)
        self.assertEqual(monitor.counts['failed_observations'],1)

    def test_invalid_limits_are_bounded_and_status_has_no_credentials(self):
        self.assertEqual(watch.CaptureMonitor(0).limit,1)
        self.assertEqual(watch.CaptureMonitor(99999).limit,2000)
        with tempfile.TemporaryDirectory() as tmp:
            monitor=watch.CaptureMonitor()
            writer=watch.ReplayWriter(Path(tmp))
            status=monitor.snapshot(2,writer)
            self.assertEqual(status['active_observations'],2)
            self.assertEqual(status['saved_games'],0)
            self.assertNotIn('account',status)
