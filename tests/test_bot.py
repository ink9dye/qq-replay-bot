# -*- coding: utf-8 -*-

import asyncio

import gc

import json

import re

import shutil

import tempfile

import unittest

import urllib.error

import urllib.request

import zipfile

from contextlib import contextmanager

from io import BytesIO

from pathlib import Path

from unittest.mock import patch



from bot.qq import OneBotClient

from bot.service import BotService, ManualClock, join_rules, subscription_accepts, system_clock

from bot.store import Store

from bot.watch_core import (

    RankRule,

    ReplayWriter,

    _rank_cache,

    apply_watch_rules,

    rank_is_within,

    rooms_from_json,

    select_rooms,

    watch_targets,

)





def sample_config(root: Path) -> dict:

    return {

        "ygopro_dir": r"D:\MyCardLibrary\ygopro",

        "data_dir": str(root / "data"),

        "replay_dir": str(root / "replays"),

        "deck_dir": str(root / "decks"),

        "mycard": {

            "host": "tiramisu.moenext.com",

            "port": 8911,

            "account": "filled-account",

            "password": "mycard-secret-value",

        },

        "qq": {

            "onebot_ws": "ws://127.0.0.1:3001",

            "access_token": "token-secret-value",

            "webui_password": "webui-secret-value",

            "admins": ["10000"],

            "users": ["10001", "10002", "10003"],

        },

        "share": {

            "bind_host": "127.0.0.1",

            "bind_port": 0,

            "public_base_url": "",

            "link_hours": 5,

        },

    }





def make_service(root: Path) -> BotService:

    return BotService(sample_config(root), root, clock=ManualClock(1_700_000_000))





class Timeline:

    """替身系统时间。生产时钟每次都读它，测试不改 service.clock.now。"""



    def __init__(self, now: float):

        self.now = float(now)



    def __call__(self) -> float:

        return self.now





@contextmanager

def temporary_service():

    root = Path(tempfile.mkdtemp())

    service = make_service(root)

    try:

        yield service

    finally:

        try:

            service.share_server.stop()

        except Exception:

            pass

        try:

            service.store.close()

        except Exception:

            pass

        gc.collect()

        shutil.rmtree(root, ignore_errors=True)





class FakeQQ:

    def __init__(self):

        self.connected = True

        self.sent = []

        self.fail_times = 0



    async def send_private(self, user_id, text: str) -> bool:

        self.sent.append((str(user_id), text))

        if self.fail_times:

            self.fail_times -= 1

            return False

        return True





def room(name, players):

    return rooms_from_json(

        {

            "rooms": [

                {

                    "roomname": name,

                    "istart": "start",

                    "users": [{"name": player, "pos": index} for index, player in enumerate(players)],

                }

            ]

        }

    )





class RuleTests(unittest.TestCase):

    def test_rank_boundaries(self):

        self.assertFalse(rank_is_within(None, 100))

        self.assertFalse(rank_is_within(0, 100))

        self.assertTrue(rank_is_within(100, 100))

        self.assertFalse(rank_is_within(101, 100))



    def test_first_rank_rule_does_not_hide_a_later_one(self):

        _rank_cache["甲"] = 8

        _rank_cache["乙"] = 500

        picked, skipped = select_rooms(

            room("mix", ["甲", "乙"]),

            [],

            "exact",

            True,

            [RankRule("甲", 10), RankRule("乙", 100)],

        )

        self.assertEqual([item.key for item in picked], ["mix"])

        self.assertEqual(skipped, [])



    def test_same_player_looser_rule_still_matches(self):

        _rank_cache["甲"] = 8

        _rank_cache["乙"] = 50

        picked, _skipped = select_rooms(

            room("mid", ["甲", "乙"]),

            [],

            "exact",

            True,

            [RankRule("甲", 10), RankRule("甲", 100)],

        )

        self.assertEqual([item.key for item in picked], ["mid"])



    def test_unknown_rank_does_not_qualify(self):

        _rank_cache["目标"] = 3

        _rank_cache["神秘"] = None

        picked, skipped = select_rooms(

            room("unknown", ["目标", "神秘"]),

            [],

            "exact",

            True,

            [RankRule("目标", 100)],

        )

        self.assertEqual(picked, [])

        self.assertTrue(skipped and "未知" in skipped[0])



    def test_rule_refresh_does_not_drop_active_rooms(self):

        with tempfile.TemporaryDirectory() as tmp:

            writer = ReplayWriter(Path(tmp))

            active = {"room-1": "running"}

            apply_watch_rules(writer, {"name_match": "exact"}, ["甲"], [RankRule("乙", 100)])

            self.assertEqual(active, {"room-1": "running"})

            self.assertEqual(writer.targets, ["甲", "乙"])

            apply_watch_rules(writer, {"name_match": "exact"}, [], [])

            self.assertEqual(active, {"room-1": "running"})

            self.assertEqual(writer.targets, [])



    def test_save_hook_runs_after_file_is_written(self):

        with tempfile.TemporaryDirectory() as tmp:

            seen = []



            def hook(paths, players, when, digest):

                for path in paths:

                    self.assertTrue(path.is_file())

                    self.assertGreater(path.stat().st_size, 0)

                seen.append(digest)



            writer = ReplayWriter(Path(tmp), ["甲"])

            writer.on_saved = hook

            saved = writer.save(b"finished-replay", ["甲", "乙"])

            self.assertEqual(len(saved), 1)

            self.assertEqual(len(seen), 1)

            self.assertTrue(str(saved[0]).endswith("甲" + "\\" + saved[0].name) or saved[0].parent.name == "甲")





class SubscriptionTests(unittest.TestCase):

    def test_merged_rules_use_each_users_own_grant(self):

        with temporary_service() as service:

            service.clock.now = 1_700_000_000

            _rank_cache["甲"] = 8

            _rank_cache["乙"] = 500

            service.handle_text("10001", '订阅 "甲" 前10 推送')

            service.handle_text("10002", '订阅 "甲" 全部 静默')

            service.handle_text("10003", '订阅 "乙" 前100 推送')

            names, rules = service.current_rules()

            self.assertEqual(names, ["甲", "乙"])

            self.assertEqual(rules, [])

            self.assertEqual(watch_targets(names, rules), ["甲", "乙"])



            writer = ReplayWriter(service.replay_root, watch_targets(names, rules))

            writer.on_saved = service.on_replay_saved

            writer.save(b"match-one", ["甲", "乙"])



            self.assertEqual(service.visible_replays("10001", "甲"), [])

            self.assertEqual(len(service.visible_replays("10002", "甲")), 1)

            self.assertEqual(len(service.visible_replays("10003", "乙")), 1)

            self.assertEqual(service.store.pending_notifications()[0]["user_id"], "10003")



            service.handle_text("10002", '推送 "甲"')

            self.assertEqual(service.store.pending_notifications()[0]["user_id"], "10003")

            writer.save(b"match-two", ["甲", "乙"])

            pending_users = [row["user_id"] for row in service.store.pending_notifications()]

            self.assertEqual(pending_users, ["10003", "10002", "10003"])



            fake = FakeQQ()

            service.qq = fake

            asyncio.run(service.deliver())

            receivers = [user for user, _text in fake.sent]

            self.assertEqual(receivers, ["10003", "10002"])
            self.assertEqual(service.store.pending_notifications(), [])

            self.assertNotIn("10001", receivers)

            self.assertTrue(all("只能在运行机器人的这台电脑上打开" in text for _user, text in fake.sent))

            self.assertEqual(service.store.pending_notifications(), [])



            service.handle_text("10001", '取消订阅 "甲"')

            self.assertIsNone(service.store.get_subscription("10001", "甲"))

            self.assertIsNotNone(service.store.get_subscription("10002", "甲"))

            names, rules = service.current_rules()

            self.assertIn("甲", names)



            reply = service.handle_text("99999", '订阅 "甲" 全部 推送')

            self.assertIn("没有使用权限", reply)

            self.assertIsNone(service.store.get_subscription("99999", "甲"))



    def test_restart_keeps_subscriptions_and_sent_state(self):

        with temporary_service() as service:

            service.clock.now = 1_700_000_000

            service.handle_text("10001", "订阅 甲 全部 推送")

            _rank_cache["甲"] = 1

            _rank_cache["乙"] = 2

            names, rules = service.current_rules()

            writer = ReplayWriter(service.replay_root, watch_targets(names, rules))

            writer.on_saved = service.on_replay_saved

            writer.save(b"kept", ["甲", "乙"])

            fake = FakeQQ()

            service.qq = fake

            asyncio.run(service.deliver())

            self.assertEqual(len(fake.sent), 1)

            path = service.store.path

            replay_id = service.store.replays_for_player("甲")[0]["id"]

            service.store.close()



            restored = Store(path)

            rows = restored.list_subscriptions("10001")

            self.assertEqual(rows[0]["player"], "甲")

            self.assertTrue(rows[0]["notify"])

            self.assertEqual(rows[0]["created_at"], 1_700_000_000)

            self.assertEqual(restored.notification_status("10001", replay_id), "sent")

            self.assertEqual(restored.pending_notifications(), [])

            restored.close()



    def test_duplicate_save_does_not_notify_twice(self):

        with temporary_service() as service:

            service.clock.now = 1_700_000_000

            service.handle_text("10001", "订阅 甲 全部 推送")

            _rank_cache["甲"] = 1

            _rank_cache["乙"] = 2

            blob = b"same-replay"

            for _index in range(2):

                writer = ReplayWriter(service.replay_root, ["甲"])

                writer.on_saved = service.on_replay_saved

                writer.save(blob, ["甲", "乙"])

            pending = service.store.pending_notifications()

            self.assertEqual(len(pending), 1)

            self.assertEqual(len(list(service.replay_root.rglob("*.yrp"))), 1)



    def test_resubscribe_keeps_start_time_until_cancelled(self):

        with temporary_service() as service:

            service.clock.now = 100

            service.handle_text("10001", "订阅 甲 全部 推送")

            service.clock.now = 500

            service.handle_text("10001", "订阅 甲 前100 静默")

            sub = service.store.get_subscription("10001", "甲")

            self.assertEqual(sub["created_at"], 100)

            self.assertEqual(sub["rank_top"], 100)

            self.assertFalse(sub["notify"])



    def test_subscribe_defaults_to_all_silent(self):

        with temporary_service() as service:

            service.clock.now = 100

            reply = service.handle_text("10001", '订阅 "甲"')

            self.assertIn("已订阅", reply)

            self.assertIn("默认", reply)

            sub = service.store.get_subscription("10001", "甲")

            self.assertIsNone(sub["rank_top"])

            self.assertFalse(sub["notify"])

            self.assertIn("全部", service.handle_text("10001", "我的订阅"))

            service.handle_text("10001", '订阅 "甲" 前100')

            sub = service.store.get_subscription("10001", "甲")

            self.assertEqual(sub["rank_top"], 100)

            self.assertFalse(sub["notify"])

            service.handle_text("10001", '订阅 "甲" 推送')

            sub = service.store.get_subscription("10001", "甲")

            self.assertIsNone(sub["rank_top"])

            self.assertTrue(sub["notify"])

            service.handle_text("10001", '取消订阅 "甲"')

            self.assertIn("没有订阅", service.handle_text("10001", "我的订阅"))


    def test_commands_and_exact_name(self):

        with temporary_service() as service:

            service.clock.now = 1_700_000_000

            reply = service.handle_text("10001", '订阅 "甲 乙" 前100 静默')

            self.assertIn("甲 乙", reply)

            self.assertIn("静默", reply)

            listed = service.handle_text("10001", "我的订阅")

            self.assertIn("前100", listed)

            direct = service.handle_text("10001", "甲 乙")

            self.assertIn("/s/", direct)

            first = direct

            service.clock.now += 10

            second = service.handle_text("10001", '录像 "甲 乙"')

            self.assertNotEqual(first, second)

            self.assertIn("5 小时", second)

            service.handle_text("10001", '取消订阅 "甲 乙"')

            self.assertIn("没有订阅", service.handle_text("10001", "我的订阅"))

            help_text = service.handle_text("88888", "帮助")

            self.assertIn("没有使用权限", help_text)



    def test_replay_before_subscription_is_shared(self):

        with temporary_service() as service:

            service.clock.now = 1_000

            service.store.upsert_subscription("10001", "甲", None, True, 1_000)

            replay, _created = service.store.add_replay(

                "old",

                "甲",

                "甲/old.yrp",

                ["甲", "乙"],

                {"甲": 1, "乙": 1},

                500,

            )

            self.assertTrue(

                subscription_accepts(service.store.get_subscription("10001", "甲"), replay)

            )

            self.assertEqual(len(service.visible_replays("10001", "甲")), 1)



    def test_startup_lines_do_not_include_secrets(self):

        with temporary_service() as service:

            text = "\n".join(service.startup_lines())

            self.assertNotIn("mycard-secret-value", text)

            self.assertNotIn("token-secret-value", text)

            self.assertNotIn("webui-secret-value", text)

            self.assertIn("已填写", text)
            self.assertIn("未订阅按对手前100", text)

            service.share.public_base_url = "http://127.0.0.1:8787"

            local = "\n".join(service.startup_lines())

            self.assertIn("本机地址", local)

            self.assertNotIn("http://127.0.0.1:8787", local)



    def test_send_failure_is_limited_and_disconnect_does_not_drop_notice(self):

        with temporary_service() as service:

            service.clock.now = 1_700_000_000

            service.handle_text("10001", "订阅 甲 全部 推送")

            _rank_cache["甲"] = 4

            _rank_cache["乙"] = 6

            writer = ReplayWriter(service.replay_root, ["甲"])

            writer.on_saved = service.on_replay_saved

            writer.save(b"notice", ["甲", "乙"])

            fake = FakeQQ()

            fake.connected = False

            service.qq = fake

            asyncio.run(service.deliver())

            self.assertEqual(fake.sent, [])

            self.assertEqual(service.store.pending_notifications()[0]["attempts"], 0)



            fake.connected = True

            fake.fail_times = 3

            asyncio.run(service.deliver())

            asyncio.run(service.deliver())

            asyncio.run(service.deliver())

            self.assertEqual(len(fake.sent), 3)

            replay_id = service.store.replays_for_player("甲")[0]["id"]

            self.assertEqual(service.store.notification_status("10001", replay_id), "failed")

            asyncio.run(service.deliver())

            self.assertEqual(len(fake.sent), 3)



    def test_join_rules_take_the_loosest_rank(self):

        subs = [

            {"player": "甲", "rank_top": 10},

            {"player": "甲", "rank_top": 100},

            {"player": "乙", "rank_top": None},

        ]

        names, rules = join_rules(subs)

        self.assertEqual(names, ["乙"])

        self.assertEqual([(rule.name, rule.top) for rule in rules], [("甲", 100)])





class ShareTests(unittest.TestCase):

    def test_expiry_boundary_downloads_and_path_escape(self):

        with temporary_service() as service:

            service.clock.now = 1_700_000_000

            service.handle_text("10001", "订阅 甲 全部 推送")

            _rank_cache["甲"] = 8

            _rank_cache["乙"] = 20

            writer = ReplayWriter(service.replay_root, ["甲"])

            writer.on_saved = service.on_replay_saved

            writer.save(b"share-body", ["甲", "乙"])

            replay_path = next(service.replay_root.rglob("*.yrp"))

            service.share_server.start()

            try:

                page = service.handle_text("10001", "录像 甲")

                token = link_token(page)

                link = service.store.get_link(token)

                self.assertEqual(link["expires_at"], link["created_at"] + 5 * 3600)

                base = f"http://127.0.0.1:{service.share.bind_port}"

                replay_id = link["replay_ids"][0]



                service.clock.now = link["expires_at"] - 1

                status, body = fetch(f"{base}/s/{token}")

                self.assertEqual(status, 200)

                self.assertIn("打包下载".encode(), body)

                status, downloaded = fetch(f"{base}/s/{token}/file/{replay_id}")

                self.assertEqual(status, 200)

                self.assertEqual(downloaded, replay_path.read_bytes())

                status, packed = fetch(f"{base}/s/{token}/zip")

                self.assertEqual(status, 200)

                self.assertTrue(packed.startswith(b"PK"))

                with zipfile.ZipFile(BytesIO(packed)) as archive:

                    self.assertEqual(archive.read(archive.namelist()[0]), b"share-body")



                before = set(service.replay_root.rglob("*.yrp"))

                writer.save(b"later-replay", ["甲", "乙"])

                added = [path for path in service.replay_root.rglob("*.yrp") if path not in before]

                status, body = fetch(f"{base}/s/{token}")

                self.assertNotIn(added[0].name.encode("utf-8"), body)

                service.clock.now += 5

                renewed = service.handle_text("10001", "录像 甲")

                new_token = link_token(renewed)

                self.assertNotEqual(new_token, token)

                self.assertEqual(service.store.get_link(token)["expires_at"], link["expires_at"])

                status, renewed_body = fetch(f"{base}/s/{new_token}")

                self.assertIn("记录到的对局".encode("utf-8"), renewed_body)
                self.assertNotIn(b'<nav',renewed_body)

                newer=service.store.replays_for_player('甲')[-1]
                status,newer_body=fetch(f"{base}/s/{new_token}/file/{newer['id']}")
                self.assertEqual(status,200)
                self.assertEqual(newer_body,added[0].read_bytes())



                service.clock.now = link["expires_at"]

                for suffix in ("", f"/file/{replay_id}", "/zip"):

                    status, body = fetch(f"{base}/s/{token}{suffix}")

                    self.assertEqual(status, 410)

                    self.assertIn("已失效".encode(), body)

                self.assertTrue(replay_path.is_file())

                self.assertEqual(replay_path.read_bytes(), b"share-body")



                outside = service.root / "secret.txt"

                outside.write_text("SECRET-MARKER", encoding="utf-8")

                escaped, _created = service.store.add_replay(

                    "escape",

                    "甲",

                    "..\\secret.txt",

                    ["甲", "乙"],

                    {"甲": 8, "乙": 20},

                    service.clock.now,

                )

                absolute, _created = service.store.add_replay(

                    "absolute",

                    "甲",

                    str(outside),

                    ["甲", "乙"],

                    {"甲": 8, "乙": 20},

                    service.clock.now,

                )

                bad = service.share.create_link("10001", "甲", [escaped["id"], absolute["id"]], 1)

                for item in (escaped["id"], absolute["id"]):

                    status, body = fetch(f"{base}/s/{bad['token']}/file/{item}")

                    self.assertEqual(status, 404)

                    self.assertNotIn(b"SECRET-MARKER", body)

                status, body = fetch(f"{base}/s/{bad['token']}/zip")

                self.assertEqual(status, 200)

                self.assertNotIn(b"SECRET-MARKER", body)

                status, body = fetch(f"{base}/s/{token}/file/999999")

                self.assertNotIn(b"SECRET-MARKER", body)

                status, body = fetch(f"{base}/s/../secret.txt")

                self.assertEqual(status, 404)

                self.assertNotIn(b"SECRET-MARKER", body)

            finally:

                service.share_server.stop()

                service.store.close()



    def test_production_clock_expires_as_real_time_advances(self):

        timeline = Timeline(1_800_000_000)

        root = Path(tempfile.mkdtemp())

        service = None

        try:

            with patch("bot.service.time.time", timeline):

                service = BotService(sample_config(root), root)

                self.assertIs(service.clock, system_clock)

                self.assertIs(service.share.clock, system_clock)

                self.assertFalse(hasattr(service.clock, "now"))

                service.handle_text("10001", "订阅 甲 全部 推送")

                _rank_cache["甲"] = 8

                _rank_cache["乙"] = 20

                writer = ReplayWriter(service.replay_root, ["甲"])

                writer.on_saved = service.on_replay_saved

                writer.save(b"live-clock", ["甲", "乙"])

                replay_path = next(service.replay_root.rglob("*.yrp"))

                service.share_server.start()

                page = service.handle_text("10001", "录像 甲")

                token = link_token(page)

                link = service.store.get_link(token)

                self.assertEqual(link["created_at"], 1_800_000_000)

                self.assertEqual(link["expires_at"], 1_800_000_000 + 5 * 3600)

                base = f"http://127.0.0.1:{service.share.bind_port}"

                replay_id = link["replay_ids"][0]



                timeline.now = link["expires_at"] - 1

                self.assertEqual(service.clock(), link["expires_at"] - 1)

                status, _body = fetch(f"{base}/s/{token}")

                self.assertEqual(status, 200)



                timeline.now = link["expires_at"]

                self.assertEqual(service.clock(), link["expires_at"])

                for suffix in ("", f"/file/{replay_id}", "/zip"):

                    status, body = fetch(f"{base}/s/{token}{suffix}")

                    self.assertEqual(status, 410)

                    self.assertIn("已失效".encode(), body)

                self.assertTrue(replay_path.is_file())

                self.assertEqual(replay_path.read_bytes(), b"live-clock")



                timeline.now = link["expires_at"] + 120

                renewed = service.handle_text("10001", "录像 甲")

                new_token = link_token(renewed)

                new_link = service.store.get_link(new_token)

                self.assertNotEqual(new_token, token)

                self.assertEqual(new_link["created_at"], timeline.now)

                self.assertEqual(new_link["expires_at"], timeline.now + 5 * 3600)

                self.assertEqual(service.store.get_link(token)["expires_at"], link["expires_at"])

        finally:

            if service is not None:

                try:

                    service.share_server.stop()

                except Exception:

                    pass

                try:

                    service.store.close()

                except Exception:

                    pass

            gc.collect()

            shutil.rmtree(root, ignore_errors=True)





class ScriptTransport:

    def __init__(self):

        self.queue = asyncio.Queue()

        self.sent = []

        self.connects = 0

        self.fail_connects = 1

        self.fail_retcodes = 1

        self.pending_events = []



    async def connect(self):

        self.connects += 1

        if self.connects <= self.fail_connects:

            raise ConnectionError("napcat down")

        for event in self.pending_events:

            self.queue.put_nowait(event)

        self.pending_events.clear()



    async def recv_text(self):

        try:

            return await asyncio.wait_for(self.queue.get(), timeout=1)

        except asyncio.TimeoutError:

            return None



    async def send_text(self, text: str):

        data = json.loads(text)

        self.sent.append(data)

        retcode = 1 if self.fail_retcodes else 0

        if self.fail_retcodes:

            self.fail_retcodes -= 1

        self.queue.put_nowait(

            json.dumps(

                {"echo": data["echo"], "retcode": retcode, "status": "ok" if retcode == 0 else "failed"},

                ensure_ascii=False,

            )

        )



    def close(self):

        self.queue.put_nowait(None)





class NapCatTests(unittest.TestCase):

    def test_reconnect_dedup_and_send_retry(self):

        async def scenario():

            transport = ScriptTransport()

            seen = []



            async def pause(_seconds):

                return None



            client = OneBotClient(

                "ws://127.0.0.1:3001",

                "token-secret-value",

                None,

                transport=transport,

                pause=pause,

                timeout=1,

            )



            async def on_event(event):

                seen.append(event["message_id"])

                ok = await client.send_private("10001", "回复")

                seen.append(ok)

                client.stop()



            client.on_event = on_event

            private = {

                "post_type": "message",

                "message_type": "private",

                "message_id": 7,

                "user_id": 10001,

                "message": "订阅 甲 全部 推送",

            }

            group = {

                "post_type": "message",

                "message_type": "group",

                "message_id": 8,

                "user_id": 10001,

                "message": "订阅 甲 全部 推送",

            }

            transport.pending_events = [

                json.dumps(private, ensure_ascii=False),

                json.dumps(private, ensure_ascii=False),

                json.dumps(group, ensure_ascii=False),

            ]

            await asyncio.wait_for(client.run(), timeout=3)

            return transport, seen, client.disconnects



        transport, seen, disconnects = asyncio.run(scenario())

        self.assertGreaterEqual(transport.connects, 2)

        self.assertGreaterEqual(disconnects, 1)

        self.assertEqual(seen, [7, True])

        self.assertEqual(len(transport.sent), 2)

        self.assertEqual(transport.sent[0]["action"], "send_private_msg")

        self.assertEqual(transport.sent[0]["params"]["message"], "回复")

        self.assertEqual(transport.fail_retcodes, 0)



    def test_three_send_failures(self):

        async def scenario():

            transport = ScriptTransport()

            transport.fail_connects = 0

            transport.fail_retcodes = 5

            result = {}



            async def pause(_seconds):

                return None



            client = OneBotClient("ws://127.0.0.1:3001", "token", None, transport=transport, pause=pause, timeout=1)



            async def on_event(_event):

                result["ok"] = await client.send_private("10001", "失败", retries=3)

                client.stop()



            client.on_event = on_event

            transport.pending_events = [

                json.dumps(

                    {

                        "post_type": "message",

                        "message_type": "private",

                        "message_id": 1,

                        "user_id": 10001,

                        "message": [{"type": "text", "data": {"text": "帮助"}}],

                    },

                    ensure_ascii=False,

                )

            ]

            await asyncio.wait_for(client.run(), timeout=3)

            return transport, result



        transport, result = asyncio.run(scenario())

        self.assertFalse(result["ok"])

        self.assertEqual(len(transport.sent), 3)





def link_token(text: str) -> str:

    matched = re.search(r"/s/([A-Za-z0-9_-]+)", text)

    if not matched:

        raise AssertionError(text)

    return matched.group(1)





def fetch(url: str) -> tuple[int, bytes]:

    try:

        with urllib.request.urlopen(url, timeout=3) as response:

            return response.status, response.read()

    except urllib.error.HTTPError as exc:

        return exc.code, exc.read()





if __name__ == "__main__":

    unittest.main()

