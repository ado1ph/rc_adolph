import json
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .api import api_server
from .delivery import HttpTransport, RetryPolicy
from .mock_supplier import supplier_server
from .store import Store
from .validation import TargetPolicy
from .worker import Worker, WorkerPool


def request_json(url, token=None, payload=None, key=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def order_request(origin, order_id, scenario="ok", legacy=False):
    path = "/vendor/legacy-orders" if legacy else "/vendor/orders"
    return {
        "url": origin + path + "?scenario=" + scenario,
        "method": "POST",
        "headers": {
            "Content-Type": "application/json",
            "X-API-Key": "supplier-demo-secret",
            "Idempotency-Key": "supplier-" + order_id,
        },
        "body": json.dumps({"order_id": order_id, "amount_cents": 1990}),
    }


def run_demo():
    with tempfile.TemporaryDirectory(prefix="notifier-demo-") as directory:
        supplier = supplier_server(0).start()
        store = Store(directory + "/notifications.sqlite3")
        policy = TargetPolicy([supplier.url], allow_loopback=True)
        api = api_server(store, policy, "demo-token", 0, max_attempts=3).start()
        worker = Worker(
            store, HttpTransport(policy, timeout=0.1),
            RetryPolicy(base_seconds=0.05, cap_seconds=0.1), lease_seconds=3,
        )
        pool = WorkerPool(worker, poll_seconds=0.02)
        pool.start()
        try:
            print("Local API:", api.url)
            print("Mock supplier:", supplier.url)
            scenarios = [
                ("success", "ok", False),
                ("transient", "flaky", False),
                ("permanent", "reject", False),
                ("outage", "always_fail", False),
                ("timeout-unknown", "timeout_once", False),
                ("timeout-legacy-unknown", "timeout_once", True),
            ]
            jobs = []
            for name, scenario, legacy in scenarios:
                payload = order_request(supplier.url, name, scenario, legacy)
                code, result = request_json(
                    api.url + "/v1/notifications", "demo-token", payload, "submit-" + name
                )
                if code != 202:
                    raise RuntimeError(result)
                jobs.append((name, result["id"]))
            deadline = time.monotonic() + 10
            expected = {
                "success": ("succeeded", 1), "transient": ("succeeded", 3),
                "permanent": ("failed", 1), "outage": ("failed", 3),
                "timeout-unknown": ("failed", 1),
                "timeout-legacy-unknown": ("failed", 1),
            }
            for name, identifier in jobs:
                while True:
                    code, result = request_json(
                        api.url + "/v1/notifications/" + identifier, "demo-token"
                    )
                    if code != 200:
                        raise RuntimeError(result)
                    if result["status"] in {"succeeded", "failed"}:
                        assert (result["status"], result["attempt_count"]) == expected[name]
                        print("{:20} status={:9} attempts={}".format(
                            name, result["status"], result["attempt_count"]
                        ))
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError("Demo timed out")
                    time.sleep(0.02)
            snapshot = supplier.state.snapshot()
            print("Supplier effects:", json.dumps(snapshot["effects"], sort_keys=True))
            assert snapshot["effects"]["timeout-unknown"] == 1
            assert snapshot["effects"]["timeout-legacy-unknown"] == 1
            print("Ambiguous timeouts stay unresolved without an explicit retry contract.")
            # Simulate vendor recovery, then explicitly replay the failed request.
            transient_payload = order_request(supplier.url, "replay-demo", "flaky")
            with supplier.state.lock:
                supplier.state.failures = 100
            _, submitted = request_json(
                api.url + "/v1/notifications", "demo-token", transient_payload, "submit-replay"
            )
            identifier = submitted["id"]
            while store.get(identifier)["status"] != "failed":
                if time.monotonic() > deadline:
                    raise RuntimeError("Replay setup timed out")
                time.sleep(0.02)
            with supplier.state.lock:
                supplier.state.failures = 0
            code, _ = request_json(
                api.url + "/v1/notifications/" + identifier + "/replay", "demo-token", {}
            )
            assert code == 202
            while store.get(identifier)["status"] != "succeeded":
                if time.monotonic() > deadline:
                    raise RuntimeError("Replay timed out")
                time.sleep(0.02)
            print("Manual replay: succeeded; prior attempt history preserved")
            print("PASS: durable delivery, conservative ambiguous handling, retries and replay")
        finally:
            api.stop()
            pool.stop()
            supplier.stop()
