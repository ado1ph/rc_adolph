import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from notifier.delivery import Result
from notifier.demo import order_request
from notifier.store import Conflict, Store
from notifier.validation import TargetPolicy, notification_request
from notifier.worker import Worker


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.0
        self.path = self.directory.name + "/db.sqlite3"
        self.store = Store(self.path, clock=lambda: self.now)
        self.policy = TargetPolicy(["http://127.0.0.1:9100"], allow_loopback=True)
        self.request, self.digest = notification_request(
            order_request("http://127.0.0.1:9100", "order-1"), self.policy
        )

    def submit(self, key="submit-1", max_attempts=3):
        return self.store.submit(key, self.request, self.digest, max_attempts)[0]

    def test_commit_survives_reopen_and_read_api_redacts_secrets(self):
        identifier = self.submit()
        restarted = Store(self.path)
        record = restarted.get(identifier)
        self.assertEqual(record["status"], "pending")
        self.assertNotIn("supplier-demo-secret", json.dumps(record))
        self.assertNotIn("request_json", record)
        self.assertNotIn("lease_token", record)
        if os.name == "posix":
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_rollback_journal_and_extra_sync_are_explicit(self):
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 3)

    def test_same_key_same_snapshot_returns_original(self):
        identifier = self.submit()
        self.assertEqual(
            self.store.submit("submit-1", self.request, self.digest, 3), (identifier, False)
        )
        with self.assertRaises(Conflict):
            self.store.submit("submit-1", self.request, "a-different-hash", 3)

    def test_concurrent_submissions_create_only_one_record(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _: self.store.submit("same-key", self.request, self.digest, 3), range(20)
            ))
        self.assertEqual(len(set(identifier for identifier, _ in results)), 1)
        self.assertEqual(sum(created for _, created in results), 1)

    def test_concurrent_claims_have_only_one_winner(self):
        self.submit()
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(lambda _: self.store.claim(10), range(20)))
        self.assertEqual(sum(job is not None for job in jobs), 1)

    def test_expired_worker_is_fenced_and_recovered(self):
        identifier = self.submit()
        old = self.store.claim(10)
        self.now += 11
        self.assertFalse(self.store.finish(old, Result(success=True, http_status=200), 0))
        new = self.store.claim(10)
        self.assertNotEqual(old["lease_token"], new["lease_token"])
        self.assertFalse(self.store.heartbeat(old, 10))
        self.assertFalse(self.store.finish(old, Result(success=True), 0))
        self.assertTrue(self.store.finish(new, Result(success=True, http_status=200), 0))
        record = self.store.get(identifier)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual([a["outcome"] for a in record["attempts"]], ["abandoned", "succeeded"])

    def test_heartbeat_extends_live_lease_but_does_not_revive_expired_one(self):
        self.submit()
        job = self.store.claim(10)
        self.now += 8
        self.assertTrue(self.store.heartbeat(job, 10))
        self.now += 5
        self.assertIsNone(self.store.claim(10))
        self.now += 6
        self.assertFalse(self.store.heartbeat(job, 10))
        self.assertIsNotNone(self.store.claim(10))

    def test_retry_schedule_and_budget(self):
        identifier = self.submit(max_attempts=2)
        first = self.store.claim(10)
        result = Result(retryable=True, http_status=503, error="http_503")
        self.store.finish(first, result, 5)
        self.assertIsNone(self.store.claim(10))
        self.now += 5
        self.store.finish(self.store.claim(10), result, 5)
        record = self.store.get(identifier)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempts"][-1]["outcome"], "retry_exhausted")
        self.now += 100
        self.assertIsNone(self.store.claim(10))

    def test_replay_preserves_history_and_resets_only_cycle_budget(self):
        identifier = self.submit(max_attempts=1)
        self.store.finish(
            self.store.claim(10), Result(http_status=400, error="http_400"), 0
        )
        self.assertTrue(self.store.replay(identifier))
        job = self.store.claim(10)
        self.assertEqual(job["attempt_count"], 2)
        self.assertEqual(job["cycle_attempts"], 1)
        self.store.finish(job, Result(success=True, http_status=200), 0)
        record = self.store.get(identifier)
        self.assertEqual(record["replay_count"], 1)
        self.assertEqual(len(record["attempts"]), 2)
        with self.assertRaises(Conflict):
            self.store.replay(identifier)
        self.assertFalse(self.store.replay("unknown"))

    def test_crash_after_last_claim_exhausts_budget_without_silent_drop(self):
        identifier = self.submit(max_attempts=1)
        self.store.claim(1)
        self.now += 2
        self.assertIsNone(self.store.claim(1))
        record = self.store.get(identifier)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempts"][0]["outcome"], "abandoned")
        self.assertEqual(record["last_error"], "worker_lease_expired")

    def test_real_process_exit_after_claim_recovers_from_disk(self):
        identifier = self.submit()
        code = (
            "import os; from notifier.store import Store; "
            "s=Store({!r},clock=lambda:1000); "
            "assert s.claim(2) is not None; os._exit(17)"
        ).format(self.path)
        result = subprocess.run([sys.executable, "-c", code], timeout=10, check=False)
        self.assertEqual(result.returncode, 17)
        self.now = 1003
        job = self.store.claim(2)
        self.assertEqual(job["id"], identifier)
        self.assertEqual(job["attempt_count"], 2)
        self.assertEqual(self.store.get(identifier)["attempts"][0]["outcome"], "abandoned")

    def test_successful_send_with_failed_local_writeback_is_recovered_not_lost(self):
        identifier = self.submit()
        transport = Mock()
        transport.send.return_value = Result(success=True, http_status=200)
        worker = Worker(self.store, transport, lease_seconds=10)
        with patch.object(self.store, "finish", side_effect=OSError("storage interrupted")):
            with self.assertRaises(OSError):
                worker.run_once()
        self.assertEqual(self.store.get(identifier)["status"], "in_flight")
        self.now += 11
        self.assertTrue(worker.run_once())
        self.assertEqual(transport.send.call_count, 2)
        record = self.store.get(identifier)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual([a["outcome"] for a in record["attempts"]], ["abandoned", "succeeded"])
