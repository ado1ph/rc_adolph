import http.client
import random
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime

from .validation import InvalidRequest

MAX_RETRY_AFTER_SECONDS = 31536000


@dataclass(frozen=True)
class Result:
    success: bool = False
    retryable: bool = False
    http_status: int = None
    error: str = None
    duration_ms: int = 0
    retry_after: float = 0
    ambiguous: bool = False
    failure_class: str = None
    cooldown_hold: bool = False
    cooldown_reason: str = None
    request_state: str = "not_sent"
    failure_phase: str = None


class LeaseLost(TimeoutError):
    pass


class ExecutionBudgetExceeded(TimeoutError):
    pass


class NetworkTimeout(Exception):
    def __init__(self, phase, request_state):
        self.phase = phase
        self.request_state = request_state


class ExecutionControl:
    def __init__(self, deadline=None):
        self.deadline = deadline
        self.lease_lost = threading.Event()
        self._outbound_lock = threading.Lock()

    def remaining(self):
        if self.lease_lost.is_set():
            raise LeaseLost("Delivery lease lost")
        if self.deadline is None:
            return None
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ExecutionBudgetExceeded("Delivery deadline exceeded")
        return remaining

    def mark_lease_lost(self):
        self.lease_lost.set()

    @contextmanager
    def outbound_guard(self):
        with self._outbound_lock:
            self.remaining()
            yield


class PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout, secure):
        super().__init__(host, port=port, timeout=timeout)
        self.ip = ip
        self.context = ssl.create_default_context() if secure else None
        self.timeout = timeout
        self.control = None
        self.on_write = None

    def _remaining(self):
        budget = self.timeout
        if self.control is not None:
            remaining = self.control.remaining()
            if remaining is not None:
                budget = min(budget, remaining)
        return budget

    def connect(self):
        timeout = self._remaining()
        try:
            self.sock = socket.create_connection((self.ip, self.port), timeout)
        except TimeoutError as exc:
            raise NetworkTimeout("connect", "not_sent") from exc
        self.sock.settimeout(self._remaining())
        if self.context is not None:
            try:
                self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
            except TimeoutError as exc:
                raise NetworkTimeout("tls", "not_sent") from exc
            self.sock.settimeout(self._remaining())

    def send(self, data):
        if self.control is None:
            if self.on_write is not None:
                self.on_write()
            try:
                super().send(data)
            except TimeoutError as exc:
                raise NetworkTimeout("write", "possibly_sent") from exc
            return
        with self.control.outbound_guard():
            if self.on_write is not None:
                self.on_write()
            try:
                super().send(data)
            except TimeoutError as exc:
                raise NetworkTimeout("write", "possibly_sent") from exc

    def abort(self):
        connection_socket = self.sock
        if connection_socket is not None:
            try:
                connection_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def retry_after_seconds(value, now=None):
    if not value:
        return 0
    try:
        value = value.strip(" \t")
        if not value:
            return 0
        if value.isascii() and value.isdigit():
            digits = value.lstrip("0") or "0"
            limit = str(MAX_RETRY_AFTER_SECONDS)
            if len(digits) > len(limit) or (len(digits) == len(limit) and digits > limit):
                return None
            seconds = int(digits)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - (time.time() if now is None else now)
        return max(0, seconds)
    except (ValueError, TypeError, OverflowError):
        return 0


class HttpTransport:
    def __init__(self, policy, timeout=3):
        self.policy = policy
        self.timeout = timeout

    def send(self, request, control=None):
        started = time.monotonic()
        connection = None
        response = None
        timer = None
        timeout_expired = threading.Event()
        request_state = "not_sent"
        failure_phase = "dns"

        def remaining_budget():
            remaining = started + self.timeout - time.monotonic()
            if control is not None:
                control_remaining = control.remaining()
                if control_remaining is not None:
                    remaining = min(remaining, control_remaining)
            if remaining <= 0:
                raise ExecutionBudgetExceeded("Delivery budget exceeded")
            return remaining

        try:
            try:
                dns_budget = remaining_budget()
                parts, origin, ip = self.policy.resolve(request["url"], timeout=dns_budget)
            except (LeaseLost, ExecutionBudgetExceeded):
                raise
            except TimeoutError as exc:
                if control is not None:
                    control.remaining()
                raise NetworkTimeout("dns", "not_sent") from exc
            remaining = remaining_budget()
            connection = PinnedConnection(
                origin[1], origin[2], ip, remaining, origin[0] == "https"
            )
            connection.control = control

            def mark_outbound():
                nonlocal request_state, failure_phase
                request_state = "possibly_sent"
                failure_phase = "write"

            connection.on_write = mark_outbound

            def abort_on_timeout():
                timeout_expired.set()
                connection.abort()

            timer = threading.Timer(remaining, abort_on_timeout)
            timer.daemon = True
            timer.start()
            connection.connect()
            failure_phase = "request"
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            remaining_budget()
            if control is not None:
                control.remaining()
            connection.request(
                request["method"], path, body=request["body"].encode("utf-8"),
                headers=request["headers"],
            )
            failure_phase = "response"
            try:
                response = connection.getresponse()
            except TimeoutError as exc:
                if control is not None and control.lease_lost.is_set():
                    raise LeaseLost("Delivery lease lost while awaiting response") from exc
                if control is not None:
                    control.remaining()
                raise NetworkTimeout("response", request_state) from exc
            status = response.status
            retry_after = retry_after_seconds(response.getheader("Retry-After"))
            cooldown_hold = retry_after is None
            request_state = "response_received"
            success = 200 <= status < 300
            failure_class = (
                None if success else "supplier_service"
                if status in {408, 425, 429} or status >= 500
                else "business_rejection"
            )
            return Result(
                success=success,
                retryable=status in {408, 425, 429} or 500 <= status < 600,
                http_status=status,
                error=(
                    None if success else
                    "retry_after_exceeds_supported_range"
                    if cooldown_hold else "http_" + str(status)
                ),
                duration_ms=int((time.monotonic() - started) * 1000),
                retry_after=0 if cooldown_hold else retry_after,
                failure_class=failure_class,
                cooldown_hold=cooldown_hold,
                cooldown_reason=(
                    "retry_after_exceeds_supported_range" if cooldown_hold else None
                ),
                request_state=request_state,
                failure_phase=failure_phase,
            )
        except InvalidRequest:
            return Result(error="target_policy_rejected", failure_class="local_policy")
        except ssl.SSLCertVerificationError:
            return Result(
                error="tls_certificate_invalid", failure_class="local_policy",
                request_state=request_state, failure_phase="tls",
            )
        except LeaseLost:
            return Result(
                error="lease_lost", duration_ms=int((time.monotonic() - started) * 1000),
                ambiguous=request_state == "possibly_sent", failure_class="local_policy",
                request_state=request_state, failure_phase=failure_phase,
            )
        except ExecutionBudgetExceeded:
            return Result(
                error="execution_budget_exhausted",
                duration_ms=int((time.monotonic() - started) * 1000),
                ambiguous=request_state == "possibly_sent", failure_class="local_policy",
                request_state=request_state, failure_phase=failure_phase,
            )
        except NetworkTimeout as exc:
            try:
                if control is not None:
                    control.remaining()
            except LeaseLost:
                return Result(
                    error="lease_lost", duration_ms=int((time.monotonic() - started) * 1000),
                    ambiguous=exc.request_state == "possibly_sent",
                    failure_class="local_policy", request_state=exc.request_state,
                    failure_phase=exc.phase,
                )
            except ExecutionBudgetExceeded:
                return Result(
                    error="execution_budget_exhausted",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    ambiguous=exc.request_state == "possibly_sent",
                    failure_class="local_policy", request_state=exc.request_state,
                    failure_phase=exc.phase,
                )
            return Result(
                retryable=exc.request_state == "not_sent",
                error="network_timeout" if exc.request_state == "not_sent"
                else "ambiguous_network_timeout",
                duration_ms=int((time.monotonic() - started) * 1000),
                ambiguous=exc.request_state == "possibly_sent",
                failure_class="supplier_network",
                request_state=exc.request_state, failure_phase=exc.phase,
            )
        except TimeoutError:
            return Result(
                error="unclassified_timeout",
                duration_ms=int((time.monotonic() - started) * 1000),
                ambiguous=request_state == "possibly_sent", failure_class="local_policy",
                request_state=request_state, failure_phase=failure_phase,
            )
        except (OSError, http.client.HTTPException):
            if control is not None:
                try:
                    control.remaining()
                except LeaseLost:
                    return Result(
                        error="lease_lost", duration_ms=int((time.monotonic() - started) * 1000),
                        ambiguous=request_state == "possibly_sent", failure_class="local_policy",
                        request_state=request_state, failure_phase=failure_phase,
                    )
                except ExecutionBudgetExceeded:
                    return Result(
                        error="execution_budget_exhausted",
                        duration_ms=int((time.monotonic() - started) * 1000),
                        ambiguous=request_state == "possibly_sent", failure_class="local_policy",
                        request_state=request_state, failure_phase=failure_phase,
                    )
            if timeout_expired.is_set():
                return Result(
                    retryable=request_state == "not_sent",
                    error="network_timeout" if request_state == "not_sent"
                    else "ambiguous_network_timeout",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    ambiguous=request_state == "possibly_sent",
                    failure_class="supplier_network",
                    request_state=request_state, failure_phase=failure_phase,
                )
            return Result(
                retryable=request_state == "not_sent",
                error="network_error" if request_state == "not_sent" else "ambiguous_network_error",
                duration_ms=int((time.monotonic() - started) * 1000),
                ambiguous=request_state == "possibly_sent",
                failure_class="supplier_network",
                request_state=request_state, failure_phase=failure_phase,
            )
        finally:
            if timer:
                timer.cancel()
            if response:
                response.close()
            if connection:
                connection.close()


@dataclass(frozen=True)
class RetryPolicy:
    base_seconds: float = 2
    cap_seconds: float = 60
    jitter_ratio: float = 0.2

    def delay(self, attempt, retry_after=0):
        ceiling = min(self.cap_seconds, self.base_seconds * (2 ** min(attempt - 1, 30)))
        delay = random.uniform(ceiling * (1 - self.jitter_ratio), ceiling)
        return max(delay, retry_after)
