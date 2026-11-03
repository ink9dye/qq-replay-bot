import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from bot.watch_api import WatchApiServer
from bot.watch_jobs import RemoteJobs

TOKEN = "test-token"


def _post(port, path, payload=None, body=None, headers=None):
    data = body if body is not None else (json.dumps(payload).encode("utf-8") if payload is not None else b"")
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST")
    request.add_header("Authorization", "Bearer " + TOKEN)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class RemoteJobsTests(unittest.TestCase):
    def test_offer_then_claim(self):
        jobs = RemoteJobs(lease=30, job_ttl=60)
        seen = {}
        thread = threading.Thread(target=lambda: seen.update(worker=jobs.offer_sync({"room": {"key": "M#1"}}, wait=5)))
        thread.start()
        deadline, job = time.time() + 5, {}
        while time.time() < deadline and not job:
            job = jobs.claim("pc-a", 1)
            time.sleep(0.01)
        thread.join(5)
        self.assertEqual(seen.get("worker"), "pc-a")
        self.assertEqual(job["room"]["key"], "M#1")
        self.assertEqual(jobs.snapshot()["stats"]["claimed"], 1)

    def test_unclaimed_offer_returns_empty(self):
        jobs = RemoteJobs(lease=30, job_ttl=60)
        self.assertEqual(jobs.offer_sync({"room": {"key": "M#2"}}, wait=0.2), "")
        self.assertEqual(jobs.snapshot()["stats"]["unclaimed"], 1)

    def test_capacity_blocks_second_claim(self):
        jobs = RemoteJobs(lease=30, job_ttl=60)
        threading.Thread(target=jobs.offer_sync, args=({"room": {"key": "A"}}, 5)).start()
        deadline, first = time.time() + 5, {}
        while time.time() < deadline and not first:
            first = jobs.claim("pc-a", 1)
            time.sleep(0.01)
        self.assertTrue(first)
        self.assertEqual(jobs.claim("pc-a", 1), {})

    def test_report_marks_done(self):
        jobs = RemoteJobs(lease=30, job_ttl=60)
        threading.Thread(target=jobs.offer_sync, args=({"room": {"key": "B"}}, 5)).start()
        deadline, job = time.time() + 5, {}
        while time.time() < deadline and not job:
            job = jobs.claim("pc-a", 1)
            time.sleep(0.01)
        self.assertTrue(jobs.report("pc-a", job["id"], "saved", {"saved": 2}))
        self.assertEqual(jobs.drain_reports()[0]["outcome"], "saved")

    def test_claimed_job_survives_ttl_but_not_lease(self):
        """长对局不能因为 job_ttl 到点被清掉；只要心跳还在就按租约续命。"""
        jobs = RemoteJobs(lease=30, job_ttl=0.05)
        threading.Thread(target=jobs.offer_sync, args=({"room": {"key": "C"}}, 5)).start()
        deadline, job = time.time() + 5, {}
        while time.time() < deadline and not job:
            job = jobs.claim("pc-a", 1)
            time.sleep(0.01)
        self.assertTrue(job)
        time.sleep(0.12)
        jobs.heartbeat("pc-a", [job["id"]])
        self.assertEqual(jobs.snapshot()["claimed"], 1)
        with jobs._lock:
            jobs._jobs[job["id"]].lease_until = time.time() - 1
        self.assertEqual(jobs.snapshot()["claimed"], 0)

    def test_per_ip_cap_switches_7_to_6(self):
        jobs = RemoteJobs(single_ip_max=7, multi_ip_max=6)
        now = time.time()
        jobs._workers["a"] = dict(active=0, ip="1.1.1.1", seen=now, capacity=1)
        self.assertEqual(jobs._ip_cap(now), 7)
        jobs._workers["b"] = dict(active=0, ip="2.2.2.2", seen=now, capacity=1)
        self.assertEqual(jobs._ip_cap(now), 6)
        self.assertEqual(jobs._online_ips(now), {"1.1.1.1", "2.2.2.2"})

    def test_active_for_ip_counts_all_workers(self):
        jobs = RemoteJobs()
        now = time.time()
        jobs._workers["a"] = dict(active=2, ip="1.1.1.1", seen=now, capacity=2)
        jobs._workers["b"] = dict(active=1, ip="1.1.1.1", seen=now, capacity=1)
        jobs._workers["c"] = dict(active=1, ip="2.2.2.2", seen=now, capacity=1)
        self.assertEqual(jobs._active_for_ip("1.1.1.1"), 3)
        self.assertEqual(jobs._active_for_ip("2.2.2.2"), 1)

    def test_heartbeat_sets_ip(self):
        jobs = RemoteJobs()
        jobs.heartbeat("pc-a", [], "1.2.3.4")
        self.assertEqual(jobs.snapshot()["workers"]["pc-a"]["ip"], "1.2.3.4")

    def test_snapshot_lists_room_and_upload_count(self):
        """设备页只读服务端事实：谁认领了哪间、这间实际收到几个录像。"""
        jobs = RemoteJobs(lease=30, job_ttl=60)
        payload = {"room": {"key": "M#room", "title": "房间", "players": ["甲", "乙"]},
                   "focus": ["甲"], "reason": "前100"}
        threading.Thread(target=jobs.offer_sync, args=(payload, 5)).start()
        deadline, job = time.time() + 5, {}
        while time.time() < deadline and not job:
            job = jobs.claim("pc-a", 1, "1.2.3.4")
            time.sleep(0.01)
        self.assertTrue(job)
        jobs.note_upload(job["id"])
        row = jobs.snapshot()["rooms"][0]
        self.assertEqual(row["worker"], "pc-a")
        self.assertEqual(row["room"], "M#room")
        self.assertEqual(row["players"], ["甲", "乙"])
        self.assertEqual(row["uploaded"], 1)
        self.assertGreater(row["claimed_at"], 0)

    def test_heartbeat_trusts_client_active_count(self):
        jobs = RemoteJobs()
        jobs.heartbeat("pc-a", ["unknown-job"], "1.2.3.4")
        self.assertEqual(jobs.snapshot()["workers"]["pc-a"]["active"], 1)
        jobs.heartbeat("pc-a", [], "1.2.3.4")
        self.assertEqual(jobs.snapshot()["workers"]["pc-a"]["active"], 0)

    def test_free_slots_respects_capacity_and_ip_cap(self):
        jobs = RemoteJobs(single_ip_max=7, multi_ip_max=6)
        now = time.time()
        jobs._workers["a"] = dict(active=0, ip="1.1.1.1", seen=now, capacity=1)
        self.assertEqual(jobs.free_slots(now), 1)
        jobs._workers["a"]["active"] = 1
        self.assertEqual(jobs.free_slots(now), 0)
        jobs._workers["b"] = dict(active=0, ip="1.1.1.1", seen=now, capacity=1)
        self.assertEqual(jobs.free_slots(now), 1)
        jobs._workers["c"] = dict(active=0, ip="2.2.2.2", seen=now, capacity=1)
        self.assertEqual(jobs.free_slots(now), 2)

    def test_two_ips_each_get_six_slots(self):
        """多个 IP 时是每个 IP 各自 6，不是总共 6。"""
        jobs = RemoteJobs(single_ip_max=7, multi_ip_max=6)
        now = time.time()
        jobs._workers["a"] = dict(active=0, ip="1.1.1.1", seen=now, capacity=6)
        jobs._workers["b"] = dict(active=0, ip="2.2.2.2", seen=now, capacity=6)
        self.assertEqual(jobs.free_slots(now), 12)
        jobs._workers["a"]["active"] = 6
        self.assertEqual(jobs.free_slots(now), 6)
        jobs._workers["b"]["active"] = 6
        self.assertEqual(jobs.free_slots(now), 0)


class WatchApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = RemoteJobs(lease=30, job_ttl=60)
        self.api = WatchApiServer(self.jobs, self.root, TOKEN, log=lambda *_: None, bind=("127.0.0.1", 0))
        self.assertTrue(self.api.start())
        self.port = self.api._httpd.server_address[1]

    def tearDown(self):
        self.api.stop()
        self.tmp.cleanup()

    def test_requires_token(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/watch-api/status")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_claim_then_upload(self):
        names = ["\u7532", "\u4e59"]
        threading.Thread(target=self.jobs.offer_sync,
                         args=({"room": {"key": "M#9", "players": names}, "focus": [names[0]]}, 5)).start()
        deadline, job = time.time() + 5, {}
        while time.time() < deadline and not job:
            status, body = _post(self.port, "/watch-api/claim", {"worker": "pc-a", "capacity": 1})
            if status == 200:
                job = json.loads(body)["job"]
            time.sleep(0.02)
        blob = b"YRP2" + b"\x00" * 32
        status, _ = _post(self.port, f"/watch-api/upload?job={job['id']}&name=abc.yrp", body=blob,
                          headers={"X-Players": urllib.parse.quote(",".join(names)), "X-Recorded-At": "1000"})
        self.assertEqual(status, 200)
        files = list(self.root.rglob("*.yrp"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), blob)
        ingest = self.jobs.drain_ingest()
        self.assertEqual(len(ingest), 1)
        self.assertEqual(ingest[0]["players"], names)

    def test_evidence_not_ingested(self):
        threading.Thread(target=self.jobs.offer_sync, args=({"room": {"key": "M#10"}, "focus": ["A"]}, 5)).start()
        deadline, job = time.time() + 5, {}
        while time.time() < deadline and not job:
            status, body = _post(self.port, "/watch-api/claim", {"worker": "pc-a", "capacity": 1})
            if status == 200:
                job = json.loads(body)["job"]
            time.sleep(0.02)
        status, _ = _post(self.port, f"/watch-api/upload?job={job['id']}&name=abc.yrp.evidence.json",
                          body=b"{}", headers={"X-Players": "A"})
        self.assertEqual(status, 200)
        self.assertEqual(list(self.root.rglob("*.evidence.json"))[0].read_bytes(), b"{}")
        self.assertEqual(self.jobs.drain_ingest(), [])

    def test_rejects_path_traversal(self):
        status, _ = _post(self.port, "/watch-api/upload?job=x&name=../evil.yrp", body=b"x")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()