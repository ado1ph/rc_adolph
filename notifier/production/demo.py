"""Run a disposable PostgreSQL-backed production API/worker demonstration."""
import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..mock_supplier import supplier_server

IMAGE = "postgres:17-alpine@sha256:eee6c6e1b0894fa4fe0075eb054af9fd1cf39a35321c2d4110626cfe506411fb"
SCENARIOS = (
    ("success", "idempotent", "ok", "succeeded"),
    ("retry_after_503", "idempotent", "flaky", "succeeded"),
    ("ambiguous_timeout_reconciled", "manual_on_ambiguous", "timeout_once", "reconciliation_required"),
)
TIMEOUT = 12
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request_json(url, method="GET", token=None, payload=None, key=None, timeout=TIMEOUT):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with HTTP.open(req, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def call(command, timeout, check=True, **kwargs):
    return subprocess.run(command, check=check, timeout=timeout, capture_output=True, text=True, **kwargs)


def wait_ready(url, token, child, deadline):
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("API process exited before readiness")
        try:
            if request_json(url + "/readyz", token=token, timeout=1)[0] == 200:
                return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(.1)
    raise TimeoutError("API readiness deadline exceeded")


def wait_status(url, token, identifier, expected, deadline):
    observed = []
    while time.monotonic() < deadline:
        status, result = request_json(
            url + "/v1/notifications/" + identifier, token=token, timeout=3,
        )
        if status == 200 and (not observed or observed[-1] != result["status"]):
            observed.append(result["status"])
        if status == 200 and result["status"] in expected:
            return result, observed
        time.sleep(.2)
    raise TimeoutError("Notification completion deadline exceeded")


def scenario_report(name, mode, observed_statuses, result, calls, effects, reused):
    return {
        "name": name,
        "mode": mode,
        "observed_statuses": observed_statuses,
        "attempts": [{
            "number": item["number"],
            "outcome": item["outcome"],
            "http_status": item.get("http_status"),
            "error": item.get("error"),
        } for item in result["attempts"]],
        "supplier_calls": calls,
        "supplier_effects": effects,
        "idempotent_receipt_reused": reused,
    }


def _write_report(path, report):
    encoded = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(encoded)


def run(output=None):
    if output is not None:
        if not Path(output).parent.is_dir():
            raise FileNotFoundError("Report output directory does not exist")
        if os.path.lexists(output):
            raise FileExistsError("Report output already exists")
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("Docker CLI is required")

    container = "notifier-demo-" + uuid.uuid4().hex
    db_name = "demo_" + secrets.token_hex(5)
    db_user = "demo_" + secrets.token_hex(5)
    password = secrets.token_urlsafe(32)
    token, reconcile_token = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    api_port = _free_port()
    children = []
    supplier = None
    report = None
    cleanup_errors = []
    with tempfile.TemporaryDirectory(prefix="notifier-demo-") as temporary:
        root = Path(temporary)
        auth, keys, credentials = (root / name for name in ("auth.json", "keys.json", "credentials.json"))
        logs = [(root / "api.log").open("w"), (root / "worker.log").open("w")]
        try:
            supplier = supplier_server(port=0).start()
            origin = supplier.url
            profiles = {}
            for name, mode, scenario, _ in SCENARIOS:
                reference = "profile_" + name
                profiles[reference] = {
                    "supplier_account": "demo-" + name, "tenants": ["demo"],
                    "origins": [origin], "retry_mode": mode,
                    "deduplication_window_seconds": 86400 if mode == "idempotent" else None,
                    "operations": [{
                        "method": "POST", "path": "/vendor/orders",
                        "query": {"scenario": [scenario]},
                    }],
                    "headers": {"X-API-Key": "supplier-demo-secret"},
                }
            _secure_json(auth, [
                _principal("sender", "sender", token),
                _principal("reconciler", "reconciler", reconcile_token),
            ])
            from cryptography.fernet import Fernet
            _secure_json(keys, {
                "encryption_keys": [Fernet.generate_key().decode()],
                "fingerprint_key": secrets.token_hex(32),
            })
            _secure_json(credentials, profiles)

            docker_env = os.environ.copy()
            docker_env["POSTGRES_PASSWORD"] = password
            call([docker, "run", "-d", "--name", container,
                  "--tmpfs", "/var/lib/postgresql/data:rw,noexec,nosuid,size=256m",
                  "-e", "POSTGRES_USER=" + db_user, "-e", "POSTGRES_DB=" + db_name,
                  "-e", "POSTGRES_PASSWORD", "-p", "127.0.0.1::5432", IMAGE],
                 timeout=180, env=docker_env)
            mapped = call([docker, "port", container, "5432/tcp"], timeout=10).stdout.strip()
            hostport = int(mapped.rsplit(":", 1)[1])
            dsn = (f"postgresql://{urllib.parse.quote(db_user)}:{urllib.parse.quote(password)}"
                   f"@127.0.0.1:{hostport}/{db_name}")
            env = {name: value for name, value in os.environ.items()
                   if not name.startswith("NOTIFY_")}
            env.update({
                "NOTIFY_ENV": "development", "NOTIFY_DATABASE_URL": dsn,
                "NOTIFY_AUTH_FILE": str(auth), "NOTIFY_KEYS_FILE": str(keys),
                "NOTIFY_CREDENTIALS_FILE": str(credentials), "NOTIFY_ALLOWED_ORIGINS": origin,
                "NOTIFY_TIMEOUT_SECONDS": "0.2", "NOTIFY_LEASE_SECONDS": "5",
                "NOTIFY_WORKERS": "2", "NOTIFY_PER_ORIGIN": "1",
            })
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if call([docker, "exec", container, "pg_isready", "-U", db_user, "-d", db_name],
                        timeout=5, check=False).returncode == 0:
                    break
                time.sleep(.25)
            else:
                raise TimeoutError("Temporary PostgreSQL readiness deadline exceeded")
            call([sys.executable, "-m", "notifier.production", "migrate"], timeout=30, env=env)
            api = subprocess.Popen(
                [sys.executable, "-m", "notifier.production", "api", "--port", str(api_port)],
                env=env, stdout=logs[0], stderr=logs[0],
            )
            children.append(api)
            worker = subprocess.Popen(
                [sys.executable, "-m", "notifier.production", "worker"],
                env=env, stdout=logs[1], stderr=logs[1],
            )
            children.append(worker)
            base = f"http://127.0.0.1:{api_port}"
            wait_ready(base, token, api, time.monotonic() + 20)

            results = []
            for name, mode, scenario, initial in SCENARIOS:
                business_id = "demo-" + uuid.uuid4().hex
                reference = "profile_" + name
                payload = {
                    "credential_ref": reference,
                    "url": origin + "/vendor/orders?scenario=" + scenario,
                    "headers": {
                        "Content-Type": "application/json",
                        **({"Idempotency-Key": "supplier-" + business_id}
                           if mode == "idempotent" else {}),
                    },
                    "body": json.dumps({"order_id": business_id, "amount_cents": 123}),
                }
                key = "submit-" + uuid.uuid4().hex
                status, receipt = request_json(base + "/v1/notifications", "POST", token, payload, key)
                _require(status == 202 and receipt.get("created") is True, "submission failed")
                identifier = receipt["id"]
                again_status, again = request_json(
                    base + "/v1/notifications", "POST", token, payload, key,
                )
                _require(again_status == 200 and again.get("created") is False
                         and again.get("id") == identifier, "submission receipt was not reused")
                expected = {"succeeded"} if initial == "succeeded" else {"reconciliation_required"}
                result, observed = wait_status(
                    base, token, identifier, expected, time.monotonic() + 70,
                )
                snap = supplier.state.snapshot()
                calls = snap["calls"].get(business_id, 0)
                effects = snap["effects"].get(business_id, 0)
                if mode == "idempotent" and scenario == "flaky":
                    _require(len(result["attempts"]) == 3 and calls == 3 and effects == 1,
                             "flaky retry evidence did not match expected 3/1")
                elif scenario == "ok":
                    _require(len(result["attempts"]) == 1 and calls == 1 and effects == 1,
                             "success evidence did not match expected 1/1")
                else:
                    _require(len(result["attempts"]) == 1 and calls == 1 and effects == 1,
                             "ambiguous timeout evidence did not match expected 1/1")
                if scenario == "timeout_once":
                    reconcile_status, reconciled = request_json(
                        base + "/v1/notifications/" + identifier + "/reconcile",
                        "POST", reconcile_token,
                        {"outcome": "delivered", "evidence_ref": "mock-receipt-" + business_id,
                         "reason": "Demo confirms supplier side effect"},
                    )
                    _require(reconcile_status == 200 and reconciled.get("status") == "succeeded",
                             "reconciliation did not close delivered notification")
                    result, later_observed = wait_status(
                        base, token, identifier, {"succeeded"}, time.monotonic() + 5,
                    )
                    if later_observed and (not observed or observed[-1] != later_observed[0]):
                        observed.extend(later_observed)
                    else:
                        observed.extend(later_observed[1:])
                results.append(scenario_report(
                    name, mode, observed, result, calls, effects, True,
                ))
            report = {
                "schema_version": 1,
                "run_at": datetime.now(timezone.utc).isoformat(),
                "scenarios": results,
            }
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    try:
                        child.terminate()
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        cleanup_errors.append("child process shutdown failed")
            if supplier is not None:
                try:
                    supplier.stop()
                except OSError:
                    cleanup_errors.append("mock supplier shutdown failed")
            for handle in logs:
                try:
                    handle.close()
                except OSError:
                    cleanup_errors.append("temporary log close failed")
            try:
                removed = call([docker, "rm", "-f", container], timeout=20, check=False)
                if removed.returncode != 0:
                    cleanup_errors.append("temporary container removal failed")
            except (subprocess.TimeoutExpired, OSError):
                cleanup_errors.append("temporary container removal failed")
            if cleanup_errors:
                raise RuntimeError(
                    "Demo cleanup incomplete; check Docker for container " + container,
                ) from None
    if report is None:
        raise RuntimeError("Demo did not produce a report")
    if output is not None:
        _write_report(output, report)
    print(json.dumps({
        "ok": True,
        "scenarios": [{
            "name": item["name"], "observed_statuses": item["observed_statuses"],
            "attempts": len(item["attempts"]),
            "supplier_calls": item["supplier_calls"],
            "supplier_effects": item["supplier_effects"],
            "idempotent_receipt_reused": item["idempotent_receipt_reused"],
        } for item in report["scenarios"]],
        "report_written": output is not None,
    }, sort_keys=True, indent=2))


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _secure_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def _principal(name, role, token):
    return {
        "name": name, "tenant": "demo", "role": role,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    }


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write a sanitized report to a new JSON file")
    args = parser.parse_args()

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        run(args.output)
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "interrupted; cleanup was attempted"}))
        return 1
    except Exception as exc:
        safe = str(exc) if isinstance(exc, (AssertionError, RuntimeError, TimeoutError,
                                            FileExistsError, FileNotFoundError)) else "demo failed"
        print(json.dumps({"ok": False, "error": safe}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
