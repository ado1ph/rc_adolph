import http.client
import json
import os
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from notifier.api import api_server
from notifier.delivery import HttpTransport, RetryPolicy
from notifier.demo import order_request, request_json
from notifier.mock_supplier import supplier_server
from notifier.store import Store
from notifier.validation import TargetPolicy
from notifier.worker import Worker, WorkerPool


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.supplier = supplier_server(0).start()
        self.policy = TargetPolicy([self.supplier.url], allow_loopback=True)
        self.store = Store(self.directory.name + "/db.sqlite3")
        self.api = api_server(self.store, self.policy, "test-token", 0, max_attempts=3).start()
        self.pool = WorkerPool(
            Worker(
                self.store, HttpTransport(self.policy, timeout=0.1),
                RetryPolicy(base_seconds=0.02, cap_seconds=0.05, jitter_ratio=0),
                lease_seconds=2,
            ), count=2, poll_seconds=0.01,
        )

    def tearDown(self):
        self.api.stop()
        self.pool.stop()
        self.supplier.stop()
        self.directory.cleanup()

    def submit(self, name="order-1", scenario="ok", legacy=False):
        payload = order_request(self.supplier.url, name, scenario, legacy)
        code, result = request_json(
            self.api.url + "/v1/notifications", "test-token", payload, "submit-" + name
        )
        self.assertEqual(code, 202, result)
        return result["id"]

    def wait_for_terminal(self, identifier):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, record = request_json(
                self.api.url + "/v1/notifications/" + identifier, "test-token"
            )
            self.assertEqual(code, 200)
            if record["status"] in {"succeeded", "failed"}:
                return record
            time.sleep(0.01)
        self.fail("Notification did not reach terminal state")

    def test_json_success_and_entry_idempotency(self):
        payload = order_request(self.supplier.url, "order-1")
        identifier = self.submit()
        code, duplicate = request_json(
            self.api.url + "/v1/notifications", "test-token", payload, "submit-order-1"
        )
        self.assertEqual(code, 200)
        self.assertEqual(duplicate["id"], identifier)
        payload["body"] = '{"order_id":"changed","amount_cents":1}'
        code, _ = request_json(
            self.api.url + "/v1/notifications", "test-token", payload, "submit-order-1"
        )
        self.assertEqual(code, 409)
        self.pool.start()
        record = self.wait_for_terminal(identifier)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["attempt_count"], 1)
        self.assertEqual(self.supplier.state.snapshot()["effects"], {"order-1": 1})

    def test_other_supplier_header_and_form_body_are_not_transformed(self):
        payload = {
            "url": self.supplier.url + "/vendor/contact",
            "headers": {
                "X-Vendor-Token": "form-demo-token",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            "body": "contact_id=customer-7&state=paid",
        }
        code, result = request_json(
            self.api.url + "/v1/notifications", "test-token", payload, "contact-7"
        )
        self.assertEqual(code, 202)
        self.pool.start()
        self.assertEqual(self.wait_for_terminal(result["id"])["status"], "succeeded")
        self.assertEqual(self.supplier.state.snapshot()["effects"], {"customer-7": 1})

    def test_transient_failure_retries_then_succeeds(self):
        identifier = self.submit(scenario="flaky")
        self.pool.start()
        record = self.wait_for_terminal(identifier)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual([a["http_status"] for a in record["attempts"]], [503, 503, 200])
        self.assertEqual(self.supplier.state.snapshot()["effects"]["order-1"], 1)

    def test_permanent_failure_is_not_retried(self):
        identifier = self.submit(scenario="reject")
        self.pool.start()
        record = self.wait_for_terminal(identifier)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempt_count"], 1)
        self.assertEqual(record["attempts"][0]["outcome"], "permanent_failure")

    def test_retries_exhaust_and_explicit_replay_recovers(self):
        with self.supplier.state.lock:
            self.supplier.state.failures = 100
        identifier = self.submit(scenario="flaky")
        self.pool.start()
        record = self.wait_for_terminal(identifier)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempt_count"], 3)
        with self.supplier.state.lock:
            self.supplier.state.failures = 0
        code, _ = request_json(
            self.api.url + "/v1/notifications/" + identifier + "/replay", "test-token", {}
        )
        self.assertEqual(code, 202)
        record = self.wait_for_terminal(identifier)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["attempt_count"], 4)
        self.assertEqual(record["replay_count"], 1)
        self.assertEqual(len(record["attempts"]), 4)

    def test_timeout_after_vendor_commit_is_not_replayed_without_declared_contract(self):
        identifier = self.submit(scenario="timeout_once")
        self.pool.start()
        record = self.wait_for_terminal(identifier)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempt_count"], 1)
        self.assertEqual(record["attempts"][0]["error"], "ambiguous_network_timeout")
        snapshot = self.supplier.state.snapshot()
        self.assertEqual(snapshot["calls"]["order-1"], 1)
        self.assertEqual(snapshot["effects"]["order-1"], 1)

    def test_ambiguous_timeout_without_vendor_idempotency_is_not_replayed(self):
        identifier = self.submit(scenario="timeout_once", legacy=True)
        self.pool.start()
        record = self.wait_for_terminal(identifier)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempt_count"], 1)
        snapshot = self.supplier.state.snapshot()
        self.assertEqual(snapshot["calls"]["order-1"], 1)
        self.assertEqual(snapshot["effects"]["order-1"], 1)

    def test_redirect_is_not_followed(self):
        identifier = self.submit(scenario="redirect")
        self.pool.start()
        record = self.wait_for_terminal(identifier)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempts"][0]["http_status"], 302)
        self.assertEqual(self.supplier.state.snapshot()["calls"]["order-1"], 1)

    def test_rate_limit_obeys_retry_after(self):
        identifier = self.submit(scenario="rate_limit")
        self.pool.start()
        record = self.wait_for_terminal(identifier)
        self.assertEqual(record["status"], "succeeded")
        first, second = record["attempts"]
        self.assertEqual(first["http_status"], 429)
        self.assertGreaterEqual(second["started_at"] - first["finished_at"], 0.99)

    def test_bad_auth_and_invalid_envelopes_do_not_enqueue(self):
        for token in [None, "wrong-token", "invalid-unicode-\u00e9"]:
            code, _ = request_json(self.api.url + "/v1/stats", token)
            self.assertEqual(code, 401)
        code, _ = request_json(
            self.api.url + "/v1/notifications", "test-token", {"url": "file:///etc/passwd"}, "x"
        )
        self.assertEqual(code, 400)
        self.assertEqual(self.store.list(), [])

    def test_health_stats_filters_and_read_redaction(self):
        self.assertEqual(request_json(self.api.url + "/healthz")[0], 200)
        identifier = self.submit()
        code, stats = request_json(self.api.url + "/v1/stats", "test-token")
        self.assertEqual(code, 200)
        self.assertEqual(stats["notifications"]["pending"], 1)
        code, listing = request_json(
            self.api.url + "/v1/notifications?status=pending&limit=1", "test-token"
        )
        self.assertEqual(code, 200)
        self.assertEqual(listing["items"][0]["id"], identifier)
        self.assertNotIn("supplier-demo-secret", json.dumps(listing))
        self.assertEqual(request_json(
            self.api.url + "/v1/notifications?limit=0", "test-token"
        )[0], 400)

    def test_accepted_jobs_survive_api_and_store_restart(self):
        identifier = self.submit()
        self.api.stop()
        self.store = Store(self.store.path)
        self.api = api_server(self.store, self.policy, "test-token", 0).start()
        self.pool.worker.store = self.store
        self.pool.start()
        self.assertEqual(self.wait_for_terminal(identifier)["status"], "succeeded")

    def test_storage_failure_never_acknowledges_or_enqueues(self):
        payload = order_request(self.supplier.url, "unaccepted")
        with patch.object(self.store, "submit", side_effect=sqlite3.OperationalError("disk full")):
            code, result = request_json(
                self.api.url + "/v1/notifications", "test-token", payload, "disk-full"
            )
        self.assertEqual(code, 503)
        self.assertNotIn("id", result)
        self.assertEqual(self.store.list(), [])

    def test_client_can_retry_after_discarding_acceptance_response(self):
        payload = order_request(self.supplier.url, "disconnected")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.api.server.server_port, timeout=2
        )
        connection.request("POST", "/v1/notifications", body=json.dumps(payload), headers={
            "Authorization": "Bearer test-token", "Idempotency-Key": "disconnected",
            "Content-Type": "application/json",
        })
        deadline = time.monotonic() + 2
        while not self.store.list():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        identifier = self.store.list()[0]["id"]
        connection.close()  # Do not consume the acknowledgement.
        code, result = request_json(
            self.api.url + "/v1/notifications", "test-token", payload, "disconnected"
        )
        self.assertEqual(code, 200)
        self.assertEqual(result["id"], identifier)
        self.assertEqual(len(self.store.list()), 1)

    def test_failed_target_does_not_block_other_jobs(self):
        failing = self.submit("broken", "always_fail")
        healthy = self.submit("healthy", "ok")
        self.pool.start()
        self.assertEqual(self.wait_for_terminal(healthy)["status"], "succeeded")
        self.assertEqual(self.wait_for_terminal(failing)["status"], "failed")

    def test_multiple_workers_deliver_each_uncontended_job_once(self):
        identifiers = [self.submit("order-" + str(index)) for index in range(15)]
        self.pool.start()
        for identifier in identifiers:
            self.assertEqual(self.wait_for_terminal(identifier)["attempt_count"], 1)
        self.assertEqual(sum(self.supplier.state.snapshot()["effects"].values()), 15)

    def test_oversized_or_chunked_envelope_is_rejected(self):
        port = self.api.server.server_port
        for headers in [
            {"Content-Length": "999999"},
            {"Transfer-Encoding": "chunked", "Content-Length": "2"},
        ]:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request("POST", "/v1/notifications", body=b"{}", headers={
                "Authorization": "Bearer test-token", "Idempotency-Key": "test",
                "Content-Type": "application/json", **headers,
            })
            self.assertEqual(connection.getresponse().status, 400)
            connection.close()
        self.assertEqual(self.store.list(), [])

    def test_cli_starts_delivers_and_shuts_down_on_sigterm(self):
        env = dict(os.environ, NOTIFY_TOKEN="cli-token", NOTIFY_ALLOWED_ORIGINS=self.supplier.url,
                   NOTIFY_ALLOW_LOOPBACK="1", NOTIFY_DB=self.directory.name + "/cli.sqlite3")
        process = subprocess.Popen(
            [sys.executable, "-m", "notifier", "serve", "--port", "0"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            readable, _, _ = select.select([process.stdout], [], [], 5)
            self.assertTrue(readable, "CLI did not announce startup")
            line = process.stdout.readline().strip()
            self.assertTrue(line.startswith("serve listening at "), line)
            origin = line.split()[3]
            code, result = request_json(
                origin + "/v1/notifications", "cli-token",
                order_request(self.supplier.url, "cli-order"), "cli-submit",
            )
            self.assertEqual(code, 202)
            deadline = time.monotonic() + 5
            while True:
                code, record = request_json(
                    origin + "/v1/notifications/" + result["id"], "cli-token"
                )
                self.assertEqual(code, 200)
                if record["status"] == "succeeded":
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
        finally:
            process.terminate()
            try:
                _, error = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
        self.assertEqual(process.returncode, 0, error)
        self.assertEqual(self.supplier.state.snapshot()["effects"]["cli-order"], 1)

    def test_cli_rejects_missing_token(self):
        env = dict(os.environ)
        env.pop("NOTIFY_TOKEN", None)
        result = subprocess.run(
            [sys.executable, "-m", "notifier", "serve"], env=env,
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("NOTIFY_TOKEN", result.stderr)
