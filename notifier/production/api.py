import asyncio
import base64
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from psycopg_pool import PoolTimeout, TooManyRequests
from starlette.concurrency import run_in_threadpool

from ..store import Conflict
from ..validation import MAX_ENVELOPE_BYTES, InvalidRequest, strict_json
from .config import Settings
from .runtime import security_for
from .store import CapacityExceeded, PayloadExpired, PostgresStore

LOGGER = logging.getLogger(__name__)


def create_app(settings=None, store=None, security=None):
    settings = settings or Settings.from_env()
    security = security or security_for(settings)
    owns_store = store is None
    store = store or PostgresStore(settings, security)

    @asynccontextmanager
    async def lifespan(app):
        if owns_store:
            await run_in_threadpool(store.open)
        try:
            if not await run_in_threadpool(store.ready):
                raise RuntimeError("Database migration is required")
            yield
        finally:
            if owns_store:
                await run_in_threadpool(store.close)

    app = FastAPI(
        title="Supplier Delivery", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    registry = CollectorRegistry()
    requests = Counter("notify_api_requests_total", "API request results", ["operation", "method", "status"], registry=registry)
    latency = Histogram(
        "notify_api_request_duration_seconds", "API request wall time", ["operation", "method"],
        buckets=(.005, .01, .025, .05, .1, .2, .5, 1, 2, 5, 10), registry=registry,
    )
    jobs_gauge = Gauge("notify_jobs", "Retained jobs by current state", ["state"], registry=registry)
    stat_gauges = {
        name: Gauge("notify_" + name, name, registry=registry)
        for name in ("oldest_pending_age_seconds", "healthy_workers", "open_circuits",
                     "snapshot_bytes", "retained_jobs", "retry_after_holds")
    }
    Gauge("notify_queue_capacity", "Global active job capacity", registry=registry).set(settings.max_pending)
    Gauge("notify_snapshot_capacity_bytes", "Encrypted snapshot byte quota", registry=registry).set(settings.max_snapshot_bytes)
    Gauge("notify_retained_capacity", "Retained job quota", registry=registry).set(settings.max_retained)

    @app.middleware("http")
    async def boundary(request, call_next):
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        status = 500

        def error(payload, code, headers=None):
            nonlocal status
            status = code
            return JSONResponse(payload, code, headers={
                "X-Request-ID": request_id, "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff", **(headers or {}),
            })

        try:
            if (len(request.headers.getlist("authorization")) > 1
                    or len(request.headers.getlist("idempotency-key")) > 1):
                return error({"error": "duplicate_security_header"}, 400)
            if request.url.path not in {"/healthz", "/readyz"}:
                principal = await run_in_threadpool(
                    security.authenticate, request.headers.get("authorization", ""),
                )
                if principal is None:
                    return error({"error": "unauthorized"}, 401, {"WWW-Authenticate": "Bearer"})
                request.state.principal = principal
                if principal.role == "metrics" and request.url.path != "/metrics":
                    return error({"error": "forbidden"}, 403)
                if request.url.path == "/metrics" and principal.role != "metrics":
                    return error({"error": "forbidden"}, 403)
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        except InvalidRequest:
            return error({"error": "invalid_request"}, 400)
        except Conflict:
            return error({"error": "idempotency_or_state_conflict"}, 409)
        except PayloadExpired:
            return error({"error": "payload_retention_expired"}, 410)
        except CapacityExceeded:
            return error({"error": "queue_capacity_exceeded"}, 429, {"Retry-After": "10"})
        except (psycopg.Error, PoolTimeout, TooManyRequests):
            return error({"error": "storage_unavailable"}, 503, {"Retry-After": "5"})
        except Exception:
            LOGGER.error("request_failed request_id=%s", request_id)
            return error({"error": "internal_error", "request_id": request_id}, 500)
        finally:
            route = request.scope.get("route")
            operation = getattr(route, "path", "unmatched")
            method = request.method if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "OTHER"
            requests.labels(operation, method, str(status)).inc()
            latency.labels(operation, method).observe(time.monotonic() - started)
            LOGGER.info("request_completed request_id=%s status=%s duration_ms=%s",
                        request_id, status, int((time.monotonic() - started) * 1000))

    async def body(request):
        if request.headers.get("content-encoding", "identity") != "identity":
            raise HTTPException(415, "Content encoding is not supported")
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "Expected application/json")
        data = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > MAX_ENVELOPE_BYTES:
                        raise HTTPException(413, "Request too large")
                    data.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, "Request body timed out")
        return strict_json(bytes(data))

    def operator(request):
        if request.state.principal.role != "operator":
            raise HTTPException(403, "Operator role required")
        return request.state.principal

    def reconciler(request):
        if request.state.principal.role != "reconciler":
            raise HTTPException(403, "Reconciler role required")
        return request.state.principal

    @app.get("/healthz")
    def health():
        return {"alive": True}

    @app.get("/readyz")
    def ready():
        if not store.ready():
            raise HTTPException(503, "Migration required")
        # API availability must not depend on workers: durable enqueue can continue
        # during a worker rollout. Worker liveness has a separate alert.
        return {"ready": True}

    @app.post("/v1/notifications")
    async def submit(request: Request):
        if request.state.principal.role == "reconciler":
            raise HTTPException(403, "Reconciler principals cannot submit notifications")
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise InvalidRequest("An idempotency key is required")
        value = await body(request)
        normalized, _ = await run_in_threadpool(security.normalize_submission, value)
        digest = await run_in_threadpool(security.fingerprint, normalized)
        old_identifier = await run_in_threadpool(
            store.lookup_submission, request.state.principal, key, digest,
        )
        if old_identifier is not None:
            location = "/v1/notifications/" + old_identifier
            return JSONResponse(
                {"id": old_identifier, "created": False, "status_url": location},
                200, headers={"Location": location},
            )
        try:
            snapshot, origin, digest = await run_in_threadpool(
                security.prepare, value, request.state.principal,
            )
        except InvalidRequest:
            # A concurrent submit may have committed while current supplier authorization
            # was being checked. Re-read the immutable receipt before returning 400.
            old_identifier = await run_in_threadpool(
                store.lookup_submission, request.state.principal, key, digest,
            )
            if old_identifier is None:
                raise
            snapshot, origin = None, None
        if old_identifier is not None:
            identifier, created = old_identifier, False
        else:
            identifier, created = await run_in_threadpool(
                store.submit, request.state.principal, key, snapshot, origin, digest,
            )
        location = "/v1/notifications/" + identifier
        return JSONResponse(
            {"id": identifier, "created": created, "status_url": location},
            202 if created else 200, headers={"Location": location},
        )

    @app.get("/v1/notifications")
    def listing(request: Request, limit: int = Query(50, ge=1, le=100),
                cursor: str | None = Query(None, max_length=256), status: str | None = None):
        if status not in {
            None, "pending", "in_flight", "succeeded", "failed", "paused",
            "reconciliation_required",
        }:
            raise InvalidRequest("Invalid status")
        before = None
        if cursor:
            try:
                before = json.loads(base64.urlsafe_b64decode(cursor))
                if not isinstance(before, list) or len(before) != 2:
                    raise ValueError()
                before = (float(before[0]), str(uuid.UUID(before[1])))
                if not 0 <= before[0] < 1e12:
                    raise ValueError()
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise InvalidRequest("Invalid cursor")
        result = store.list(request.state.principal.tenant, before, limit, status)
        if result["next_cursor"]:
            result["next_cursor"] = base64.urlsafe_b64encode(json.dumps(result["next_cursor"]).encode()).decode()
        return result

    @app.get("/v1/notifications/{identifier}")
    def get(identifier: uuid.UUID, request: Request, after_attempt: int = Query(0, ge=0),
            limit: int = Query(50, ge=1, le=100)):
        result = store.get(request.state.principal.tenant, identifier, after_attempt, limit)
        if result is None:
            raise HTTPException(404, "Notification not found")
        return result

    @app.post("/v1/notifications/{identifier}/replay")
    async def replay(identifier: uuid.UUID, request: Request):
        principal = operator(request)
        value = await body(request)
        if (not isinstance(value, dict) or set(value) != {"reason"}
                or not isinstance(value["reason"], str) or not 1 <= len(value["reason"].strip()) <= 256
                or any(ord(c) < 32 for c in value["reason"])):
            raise InvalidRequest("A bounded, nonempty audit reason is required")
        result = await run_in_threadpool(store.replay, principal, identifier, value["reason"])
        if result is False:
            raise HTTPException(404, "Notification not found")
        if isinstance(result, str) and result == "reconciliation_required":
            raise HTTPException(
                409, "Supplier key safety window elapsed; reconcile before retrying",
            )
        return JSONResponse({"id": str(identifier), "status": "pending"}, 202)

    @app.post("/v1/notifications/{identifier}/reconcile")
    async def reconcile(identifier: uuid.UUID, request: Request):
        principal = reconciler(request)
        value = await body(request)
        if (not isinstance(value, dict)
                or not {"outcome", "evidence_ref", "reason"} <= set(value)
                or set(value) - {
                    "outcome", "evidence_ref", "reason", "retry_after_reconciliation",
                }
                or not isinstance(value["outcome"], str)
                or value["outcome"] not in {"delivered", "not_delivered"}
                or ("retry_after_reconciliation" in value
                    and type(value["retry_after_reconciliation"]) is not bool)
                or (value["outcome"] == "delivered"
                    and value.get("retry_after_reconciliation") is True)
                or not isinstance(value["evidence_ref"], str)
                or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", value["evidence_ref"])
                or not isinstance(value["reason"], str)
                or not 1 <= len(value["reason"].strip()) <= 256
                or any(ord(char) < 32 for char in value["reason"])):
            raise InvalidRequest("A reconciliation outcome, evidence reference and reason are required")
        result = await run_in_threadpool(
            store.reconcile, principal, identifier,
            value["outcome"], value["evidence_ref"], value["reason"],
            value.get("retry_after_reconciliation"),
        )
        if result is None:
            raise HTTPException(404, "Notification not found")
        return JSONResponse(result, 202 if result["status"] == "pending" else 200)

    @app.get("/v1/stats")
    def stats(request: Request):
        return store.stats(request.state.principal.tenant)

    @app.get("/v1/audit")
    def audit(request: Request, before: int = Query(None, ge=1), limit: int = Query(50, ge=1, le=100)):
        principal = operator(request)
        return store.audit(principal.tenant, before, limit)

    @app.get("/metrics")
    def metrics():
        # DB-derived gauges remain correct across process restarts and API replicas.
        stats = store.stats()
        for state, count in stats["notifications"].items():
            jobs_gauge.labels(state).set(count)
        for name, gauge in stat_gauges.items():
            gauge.set(stats[name])
        return Response(generate_latest(registry), media_type="text/plain; version=0.0.4")

    return app
