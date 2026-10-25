import hmac
import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .store import Conflict
from .validation import InvalidRequest, MAX_ENVELOPE_BYTES, notification_request, strict_json


IDENTIFIER = r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # The socket may disconnect after committing a submission; its retry uses the same key.
        pass


class BackgroundServer:
    def __init__(self, server):
        self.server = server
        self.thread = None

    @property
    def url(self):
        host, port = self.server.server_address[:2]
        return "http://{}:{}".format(host, port)

    def start(self):
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        return self

    def stop(self):
        if self.thread:
            self.server.shutdown()
            self.thread.join()
        self.server.server_close()


def api_server(store, policy, token, port=9000, max_attempts=5):
    if not token:
        raise ValueError("NOTIFY_TOKEN is required")
    if not 1 <= max_attempts <= 100:
        raise ValueError("max_attempts must be between 1 and 100")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Notifier"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass

        def respond(self, status, payload, location=None):
            body = json.dumps(payload, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def read_json(self):
            if self.headers.get("Transfer-Encoding"):
                raise InvalidRequest("Transfer-Encoding is not accepted")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,6}", lengths[0]):
                raise InvalidRequest("Exactly one valid Content-Length is required")
            length = int(lengths[0])
            if not 0 < length <= MAX_ENVELOPE_BYTES:
                raise InvalidRequest("Request body must be between 1 byte and 256 KiB")
            if self.headers.get_content_type() != "application/json":
                raise InvalidRequest("Content-Type must be application/json")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise InvalidRequest("Incomplete request body")
            return strict_json(raw)

        def authorized(self):
            values = self.headers.get_all("Authorization", [])
            return len(values) == 1 and hmac.compare_digest(
                values[0].encode("utf-8"), ("Bearer " + token).encode("utf-8")
            )

        def dispatch(self):
            path = urlsplit(self.path).path
            if self.command == "GET" and path == "/healthz":
                store.stats()
                self.respond(200, {"status": "ok"})
                return
            if not self.authorized():
                self.respond(401, {"error": "Unauthorized"})
                return
            if self.command == "POST" and path == "/v1/notifications":
                keys = self.headers.get_all("Idempotency-Key", [])
                if len(keys) != 1 or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", keys[0]):
                    raise InvalidRequest("Idempotency-Key must be 1-128 safe ASCII characters")
                request, digest = notification_request(self.read_json(), policy)
                identifier, created = store.submit(keys[0], request, digest, max_attempts)
                location = "/v1/notifications/" + identifier
                self.respond(202 if created else 200, {
                    "id": identifier, "created": created, "status_url": location,
                }, location)
                return
            match = re.fullmatch("/v1/notifications/" + IDENTIFIER, path)
            if self.command == "GET" and match:
                result = store.get(match.group(1))
                self.respond(200 if result else 404, result or {"error": "Not found"})
                return
            match = re.fullmatch("/v1/notifications/" + IDENTIFIER + "/replay", path)
            if self.command == "POST" and match:
                if self.read_json() != {}:
                    raise InvalidRequest("Replay body must be an empty object")
                exists = store.replay(match.group(1))
                self.respond(202 if exists else 404, (
                    {"id": match.group(1), "status": "pending"} if exists else {"error": "Not found"}
                ))
                return
            if self.command == "GET" and path == "/v1/notifications":
                query = parse_qs(urlsplit(self.path).query)
                status = query.get("status", [None])[0]
                if status not in {None, "pending", "in_flight", "succeeded", "failed"}:
                    raise InvalidRequest("Invalid status filter")
                try:
                    limit = int(query.get("limit", ["50"])[0])
                except ValueError as exc:
                    raise InvalidRequest("Invalid limit") from exc
                if not 1 <= limit <= 100:
                    raise InvalidRequest("limit must be between 1 and 100")
                self.respond(200, {"items": store.list(status, limit)})
                return
            if self.command == "GET" and path == "/v1/stats":
                self.respond(200, store.stats())
                return
            self.respond(404, {"error": "Not found"})

        def handle_request(self):
            try:
                self.dispatch()
            except InvalidRequest as exc:
                self.respond(400, {"error": str(exc)})
            except Conflict as exc:
                self.respond(409, {"error": str(exc)})
            except sqlite3.Error:
                self.respond(503, {"error": "Storage unavailable; retry with the same key"})
            except (TimeoutError, OSError):
                self.respond(408, {"error": "Request timed out or disconnected"})
            except Exception:
                self.respond(500, {"error": "Internal error"})

        do_POST = handle_request
        do_GET = handle_request

    return BackgroundServer(LocalServer(("127.0.0.1", port), Handler))
