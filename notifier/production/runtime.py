import ipaddress
import json
import logging
import time
from dataclasses import replace

import dns.exception
import dns.resolver

from ..delivery import ExecutionBudgetExceeded, HttpTransport, LeaseLost, Result
from ..validation import InvalidRequest, TargetPolicy
from .config import Security


def bounded_dns(host, timeout=2):
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    resolver = dns.resolver.Resolver()
    deadline = time.monotonic() + timeout
    addresses = []
    try:
        for kind in ("A", "AAAA"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("DNS deadline exceeded")
            try:
                answer = resolver.resolve(host, kind, lifetime=remaining, search=False)
                addresses.extend(str(record) for record in answer)
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                continue
    except dns.exception.DNSException as exc:
        raise OSError("DNS lookup failed") from exc
    if not addresses:
        raise OSError("DNS returned no addresses")
    return list(dict.fromkeys(addresses))


def security_for(settings):
    policy = TargetPolicy(settings.origins, settings.development, resolver=bounded_dns)
    return Security(
        settings.auth_file, settings.keys_file, settings.credentials_file, policy,
        settings.development,
    )


class SupplierTransport:
    def __init__(self, security, timeout):
        self.security = security
        self.http = HttpTransport(security.policy, timeout)

    def send(self, request, control=None):
        try:
            if control is not None:
                control.remaining()
            materialized = self.security.materialize(request["snapshot"], request["tenant"])
        except LeaseLost:
            return Result(error="lease_lost", failure_class="local_policy")
        except ExecutionBudgetExceeded:
            return Result(error="execution_budget_exhausted", failure_class="local_policy")
        except InvalidRequest:
            return Result(error="credential_authorization_rejected",
                          failure_class="local_authorization")
        except (OSError, ValueError, KeyError, TypeError):
            # Mounted credential rotation/configuration errors must not leak secret values.
            return Result(retryable=True, error="credential_configuration_unavailable",
                          failure_class="local_configuration")
        if control is not None:
            try:
                control.remaining()
            except LeaseLost:
                return Result(
                    error="lease_lost",
                    failure_class="local_policy",
                )
            except ExecutionBudgetExceeded:
                return Result(
                    error="execution_budget_exhausted",
                    failure_class="local_policy",
                )
        result = self.http.send(materialized, control=control)
        mode = request["snapshot"].get("_retry_mode", "manual_on_ambiguous")
        uncertain = result.ambiguous or result.request_state == "possibly_sent"
        if uncertain:
            if mode == "manual_on_ambiguous":
                return replace(result, retryable=False, error="manual_reconciliation_required")
            if mode == "duplicate_tolerant":
                return replace(result, retryable=True)
            stable_key = request["snapshot"].get("headers", {}).get("idempotency-key")
            wire_key = materialized.get("headers", {}).get("idempotency-key")
            key_window = request["snapshot"].get("_key_window_seconds")
            if (not isinstance(stable_key, str) or not stable_key
                    or stable_key != wire_key or type(key_window) is not int or key_window < 60):
                return replace(result, retryable=False, error="manual_reconciliation_required")
            return replace(result, retryable=True)
        unsafe_response = (
            result.retryable and result.http_status is not None
        )
        if mode == "manual_on_ambiguous" and unsafe_response:
            return replace(result, retryable=False, error="manual_reconciliation_required")
        return result


class JsonLogFormatter(logging.Formatter):
    def format(self, record):
        # Do not attach exception text, request paths, request bodies or headers.
        return json.dumps({
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        })


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL)
