"""HTTP task commit boundaries; no credentials are stored in the ownership ledger."""
from contextlib import contextmanager
import hashlib
import sqlite3
import threading
import time
from .watch_task_state import TaskOwners, AuthorizationExpired


class TaskLifecycle:
    def __init__(self, app):
        self.app = app
        self.jobs = app.jobs
        self.boot_at = time.time()
        self.legacy_generation = 'legacy:' + hashlib.sha256(app.token.encode()).hexdigest()[:16]
        self.owners = TaskOwners(app.replay_root.parent / 'watch-task-owners.sqlite3')
        self.jobs.on_release = self.owners.finish
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None

    def coordination(self):
        return self.app.token_store.lifecycle_lock() if self.app.token_store else self.lock

    @contextmanager
    def authorized(self, auth):
        if not auth:
            raise AuthorizationExpired('unauthorized')
        if auth.get('id') is not None:
            if self.app.token_store is None:
                raise AuthorizationExpired('unauthorized')
            with self.app.token_store.authorize_id(auth['id'], auth['worker']):
                yield
        else:
            with self.coordination():
                if not self.app.token or auth['generation'] != self.legacy_generation:
                    raise AuthorizationExpired('unauthorized')
                yield

    def owns(self, auth, worker, job_id, adopt=False):
        generation = auth['generation']
        row = self.owners.get(job_id)
        if row:
            return row if row['worker'] == worker and row['generation'] == generation else None
        with self.jobs._lock:
            job = self.jobs._jobs.get(job_id)
            if job and (job.claimed_by != worker or job.generation not in ('', generation)):
                return None
        # The privileged legacy credential gets a bounded migration window.
        # Device tokens never adopt an unproven job, even when issued before
        # collector boot: reissue during downtime would otherwise inherit it.
        # All new device claims have durable ownership across future restarts.
        if (adopt and time.time() <= self.boot_at + self.jobs.lease
                and auth.get('id') is None
                and float(auth.get('created_at') or 0) <= self.boot_at):
            if self.owners.remember(job_id, worker, generation, self.jobs.lease):
                return self.owners.get(job_id)
        return None

    @contextmanager
    def upload_authorized(self, auth, job_id):
        with self.authorized(auth):
            row = self.owners.get(job_id)
            worker = auth.get('worker') or (row['worker'] if row else self.jobs.upload_source(job_id))
            if not worker and auth.get('id') is None:
                worker = 'legacy-unreported'
            if self.owns(auth, worker, job_id, adopt=True) is None:
                raise PermissionError('job_owner_mismatch')
            # Valid owners can submit buffered data after lease/completion;
            # uploads alone never renew a lease or revive capacity.
            yield

    def claim(self, worker, capacity, ip, wait, auth):
        deadline = time.monotonic() + max(0., min(30., wait))
        while True:
            with self.authorized(auth):
                payload = self.jobs.claim(worker, capacity, ip, 0, generation=auth['generation'])
                if payload:
                    if not self.owners.remember(payload['id'], worker, auth['generation'], self.jobs.lease):
                        self.jobs.cancel(payload['id'])
                        raise PermissionError('job_owner_mismatch')
                    return payload
            if time.monotonic() >= deadline or self.stop_event.is_set():
                return {}
            self.stop_event.wait(min(.2, max(0., deadline-time.monotonic())))

    def heartbeat(self, worker, active, ip, auth):
        with self.authorized(auth):
            accepted = []; cancel = []
            supplied = active is not None
            if not supplied:
                with self.jobs._lock:
                    active = list(self.jobs._heartbeat_jobs.get(worker, set()) | {
                        key for key, job in self.jobs._jobs.items()
                        if job.claimed_by == worker and job.generation == auth['generation']})
            if active is not None:
                if len(active) > 64:
                    raise ValueError('too_many_active_jobs')
                for key in set(active):
                    row = self.owns(auth, worker, key, adopt=True)
                    if row and row['state'] == 'active' and row['expires'] >= time.time():
                        self.owners.remember(key, worker, auth['generation'], self.jobs.lease)
                        accepted.append(key)
                    else:
                        cancel.append(key)
            self.jobs.heartbeat(worker, accepted if supplied else None, ip, generation=auth['generation'])
            return cancel

    def report(self, worker, job_id, outcome, detail, auth):
        with self.authorized(auth):
            row = self.owns(auth, worker, job_id)
            if row is None:
                raise PermissionError('job_owner_mismatch')
            if row['state'] != 'active':
                return True
            ok = self.jobs.report(worker, job_id, outcome, detail, generation=auth['generation'], verified_owner=True)
            if ok:
                self.owners.finish(job_id, worker, auth['generation'], 'completed')
                self.app.log(f'[远程] {worker} 结束任务 {job_id}：{outcome or "unknown"}')
            return ok

    def reconcile(self):
        """Finite indexed batches, including when no client reconnects."""
        with self.coordination():
            records = self.owners.outstanding()
            generations = self.jobs.generations()
            ids = {int(g.split(':')[1]) for _,g in generations if g.startswith('device:')}
            ids.update(int(r['generation'].split(':')[1]) for r in records if r['generation'].startswith('device:'))
            valid = self.app.token_store.valid_ids(ids) if self.app.token_store else set()
            for worker,generation in generations:
                if generation.startswith('device:') and int(generation.split(':')[1]) not in valid:
                    self.jobs.invalidate(worker,generation)
            now = time.time()
            for row in records:
                generation = row['generation']
                invalid = generation.startswith('device:') and int(generation.split(':')[1]) not in valid
                if invalid or row['expires'] < now:
                    self.jobs.release_job(row['worker'],generation,row['job'],'token_invalid'if invalid else'lease_expired')
                    self.owners.finish(row['job'],row['worker'],generation,'token_invalid'if invalid else'lease_expired')
            self.jobs.snapshot()
            self.owners.cleanup()

    def start(self):
        self.stop_event.clear()
        def run():
            failed = False
            while not self.stop_event.wait(5):
                try:
                    self.reconcile(); failed = False
                except (OSError, sqlite3.Error, ValueError):
                    if not failed:
                        self.app.log('[远程] 任务身份对账暂不可用；请求提交继续逐次验证')
                    failed = True
        self.thread = threading.Thread(target=run, name='watch-task-reconcile', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=6)
            self.thread = None
