import hashlib
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler

from .api import BackgroundServer, LocalServer


class SupplierState:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = {}
        self.effects = {}
        self.receipts = {}
        self.failures = 2

    def snapshot(self):
        with self.lock:
            return {"calls": dict(self.calls), "effects": dict(self.effects)}


def supplier_server(port=9100, state=None):
    state = state or SupplierState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, payload, headers=None):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == "/admin/state":
                self.respond(200, state.snapshot())
            else:
                self.respond(404, {"error": "Not found"})

        def do_POST(self):
            parts = urlsplit(self.path)
            if parts.path not in {"/vendor/orders", "/vendor/legacy-orders", "/vendor/contact"}:
                self.respond(404, {"error": "Not found"})
                return
            length = self.headers.get("Content-Length", "0")
            if not length.isdigit() or not 0 < int(length) <= 128 * 1024:
                self.respond(400, {"error": "Invalid length"})
                return
            body = self.rfile.read(int(length))
            if parts.path == "/vendor/contact":
                if self.headers.get("X-Vendor-Token") != "form-demo-token":
                    self.respond(401, {"error": "Invalid vendor token"})
                    return
                if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                    self.respond(415, {"error": "Expected a form"})
                    return
                data = parse_qs(body.decode("utf-8"))
                business_id = data.get("contact_id", [""])[0]
                valid = bool(business_id) and data.get("state") == ["paid"]
            else:
                if self.headers.get("X-API-Key") != "supplier-demo-secret":
                    self.respond(401, {"error": "Invalid supplier API key"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self.respond(415, {"error": "Expected JSON"})
                    return
                try:
                    data = json.loads(body)
                    business_id = data.get("order_id", "")
                    valid = (
                        isinstance(business_id, str) and bool(business_id)
                        and type(data.get("amount_cents")) is int and data["amount_cents"] > 0
                    )
                except (ValueError, AttributeError):
                    valid = False
            if not valid:
                self.respond(400, {"error": "Invalid vendor payload"})
                return
            query = parse_qs(parts.query)
            scenario = query.get("scenario", ["ok"])[0]
            key = self.headers.get("Idempotency-Key")
            digest = hashlib.sha256(body).hexdigest()
            receipt_key = (parts.path, key) if key and parts.path != "/vendor/legacy-orders" else None
            reply = (200, {"accepted": True, "business_id": business_id}, {})
            sleep = False
            with state.lock:
                state.calls[business_id] = state.calls.get(business_id, 0) + 1
                number = state.calls[business_id]
                previous = state.receipts.get(receipt_key) if receipt_key else None
                if previous:
                    reply = (
                        (200, {"accepted": True, "deduplicated": True}, {})
                        if previous == digest else (409, {"error": "Idempotency conflict"}, {})
                    )
                elif scenario == "reject":
                    reply = (400, {"error": "Permanent rejection"}, {})
                elif scenario == "always_fail" or (
                    scenario == "flaky" and number <= state.failures
                ):
                    reply = (503, {"error": "Temporary outage"}, {})
                elif scenario == "rate_limit" and number == 1:
                    reply = (429, {"error": "Rate limited"}, {"Retry-After": "1"})
                elif scenario == "redirect":
                    reply = (302, {}, {"Location": "/vendor/orders"})
                else:
                    state.effects[business_id] = state.effects.get(business_id, 0) + 1
                    if receipt_key:
                        state.receipts[receipt_key] = digest
                    # Commit BEFORE sleeping to model a real ambiguous timeout.
                    sleep = scenario == "timeout_once" and number == 1
            if sleep:
                time.sleep(0.5)
            self.respond(*reply)

    server = BackgroundServer(LocalServer(("127.0.0.1", port), Handler))
    server.state = state
    return server
