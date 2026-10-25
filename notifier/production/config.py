import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, unquote, urlsplit

from cryptography.fernet import Fernet, MultiFernet

from ..validation import InvalidRequest, TargetPolicy, notification_request, split_target


def read_json(path):
    data = Path(path).read_bytes()
    if len(data) > 65536:
        raise ValueError("Configuration file exceeds 64 KiB")
    return json.loads(data)


def origin_key(origin):
    scheme, host, port = origin
    return f"{scheme}://[{host}]:{port}" if ":" in host else f"{scheme}://{host}:{port}"


@dataclass(frozen=True)
class Principal:
    name: str
    tenant: str
    role: str


class Security:
    def __init__(self, auth_file, keys_file, credentials_file, policy, development=False):
        self.auth_file = auth_file
        self.keys_file = keys_file
        self.credentials_file = credentials_file
        self.policy = policy
        self.development = development
        # Validate mounted configuration before the process becomes ready.
        self._clients()
        self.cipher()
        if not isinstance(read_json(credentials_file), dict):
            raise ValueError("Credentials must be an object")

    def _clients(self):
        clients = read_json(self.auth_file)
        if not isinstance(clients, list) or not clients:
            raise ValueError("At least one API principal is required")
        names, hashes = set(), set()
        for client in clients:
            if set(client) != {"name", "tenant", "role", "token_sha256"}:
                raise ValueError("Invalid principal configuration")
            if not all(isinstance(client[k], str) for k in client):
                raise ValueError("Principal fields must be strings")
            if not all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", client[k])
                       for k in ("name", "tenant")):
                raise ValueError("Invalid principal name or tenant")
            if client["role"] not in {"sender", "operator", "reconciler", "metrics"}:
                raise ValueError("Invalid role")
            if not re.fullmatch(r"[a-f0-9]{64}", client["token_sha256"]):
                raise ValueError("Tokens must be stored as SHA-256 hashes")
            if client["name"] in names or client["token_sha256"] in hashes:
                raise ValueError("Duplicate principal or token")
            names.add(client["name"])
            hashes.add(client["token_sha256"])
        return clients

    def authenticate(self, authorization):
        if not authorization.startswith("Bearer ") or len(authorization) > 512:
            return None
        token = authorization[7:]
        if len(token) < 32:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        matched = None
        for client in self._clients():
            if hmac.compare_digest(digest, client["token_sha256"]):
                matched = Principal(client["name"], client["tenant"], client["role"])
        return matched

    def cipher(self):
        config = read_json(self.keys_file)
        keys = config["encryption_keys"]
        if not isinstance(keys, list) or not 1 <= len(keys) <= 8:
            raise ValueError("One to eight encryption keys are required")
        if not re.fullmatch(r"[a-f0-9]{64}", config["fingerprint_key"]):
            raise ValueError("A stable 256-bit fingerprint key is required")
        return MultiFernet([Fernet(key.encode()) for key in keys])

    def fingerprint(self, request):
        key = bytes.fromhex(read_json(self.keys_file)["fingerprint_key"])
        caller_request = {name: value for name, value in request.items() if not name.startswith("_")}
        value = json.dumps(caller_request, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(key, value, hashlib.sha256).hexdigest()

    def supplier_contract_fingerprint(self, entry):
        immutable = {
            "supplier_account": entry["supplier_account"],
            "tenants": sorted(entry["tenants"]),
            "origins": sorted(entry["origins"]),
            "operations": sorted(
                entry["operations"],
                key=lambda operation: json.dumps(operation, sort_keys=True, separators=(",", ":")),
            ),
            "retry_mode": entry.get("retry_mode", "manual_on_ambiguous"),
            "deduplication_window_seconds": entry.get("deduplication_window_seconds"),
        }
        key = bytes.fromhex(read_json(self.keys_file)["fingerprint_key"])
        value = json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(key, b"supplier-contract-v1\0" + value, hashlib.sha256).hexdigest()

    def encode(self, request):
        return self.cipher().encrypt(json.dumps(request).encode()).decode()

    def decode(self, value):
        return json.loads(self.cipher().decrypt(value.encode()))

    def supplier_contract(self, reference, tenant, origin):
        entry = read_json(self.credentials_file).get(reference)
        if not entry or tenant not in entry.get("tenants", []):
            raise InvalidRequest("Credential reference is not authorized")
        if (not isinstance(entry.get("supplier_account"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", entry["supplier_account"])):
            raise InvalidRequest("Supplier contract must identify its verified supplier account")
        allowed = TargetPolicy(entry.get("origins", []), self.development)
        if origin not in allowed.origins:
            raise InvalidRequest("Credential reference does not match the destination")
        mode = entry.get("retry_mode", "manual_on_ambiguous")
        if mode not in {"idempotent", "duplicate_tolerant", "manual_on_ambiguous"}:
            raise InvalidRequest("Invalid supplier retry safety configuration")
        operations = entry.get("operations")
        if not isinstance(operations, list) or not operations:
            raise InvalidRequest("Supplier contract must define allowed HTTP operations")
        for operation in operations:
            if (not isinstance(operation, dict) or set(operation) != {"method", "path", "query"}
                    or not isinstance(operation["method"], str)
                    or operation["method"] not in {"POST", "PUT", "PATCH"}
                    or not self._valid_path_template(operation["path"])
                    or not self._valid_query_template(operation["query"])):
                raise InvalidRequest("Invalid supplier operation allowlist")
        return entry

    @staticmethod
    def _valid_path_template(path):
        if not isinstance(path, str) or not path.startswith("/") or "?" in path or "#" in path:
            return False
        if any(char in path for char in ("%", "\\", "\x00")) or "//" in path:
            return False
        segments = path.split("/")[1:]
        if path == "/":
            return True
        if any(segment in {"", ".", ".."} for segment in segments):
            return False
        for segment in segments:
            if "{" in segment or "}" in segment:
                if not re.fullmatch(r"\{[A-Za-z][A-Za-z0-9_]*\}", segment):
                    return False
            elif not re.fullmatch(r"[A-Za-z0-9._~-]+", segment):
                return False
        return True

    @staticmethod
    def _valid_query_template(query):
        if not isinstance(query, dict) or len(query) > 16:
            return False
        for name, values in query.items():
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.~-]{1,64}", name)
                    or not isinstance(values, list) or not values
                    or any(not isinstance(value, str) or len(value) > 128 for value in values)):
                return False
        return True

    @classmethod
    def _operation_allowed(cls, entry, method, path, query_string):
        if not cls._valid_path_template(path) or "%" in path:
            return False
        actual = [] if path == "/" else path.split("/")[1:]
        try:
            query_pairs = parse_qsl(
                query_string, keep_blank_values=True, strict_parsing=True, max_num_fields=32,
            ) if query_string else []
        except ValueError:
            return False
        query_names = [name for name, _ in query_pairs]
        if len(set(query_names)) != len(query_names):
            return False
        for operation in entry["operations"]:
            if method != operation["method"]:
                continue
            pattern = [] if operation["path"] == "/" else operation["path"].split("/")[1:]
            if len(pattern) != len(actual):
                continue
            try:
                decoded = [unquote(segment, errors="strict") for segment in actual]
            except UnicodeError:
                continue
            if any(segment in {"", ".", ".."} or "/" in segment or "\\" in segment
                   or any(ord(char) < 32 for char in segment) for segment in decoded):
                continue
            if all(
                re.fullmatch(r"\{[A-Za-z][A-Za-z0-9_]*\}", expected)
                or expected == value
                for expected, value in zip(pattern, decoded)
            ) and cls._query_matches(operation["query"], query_pairs):
                return True
        return False

    @staticmethod
    def _query_matches(allowed, actual):
        if set(name for name, _ in actual) != set(allowed):
            return False
        for name, value in actual:
            permitted = allowed[name]
            if value not in permitted and not any(
                re.fullmatch(r"\{[A-Za-z][A-Za-z0-9_]*\}", item) and value for item in permitted
            ):
                return False
        return True

    def credential_headers(self, reference, tenant, origin):
        return self.supplier_contract(reference, tenant, origin).get("headers", {})

    def prepare(self, value, principal):
        request, origin_key_value = self.normalize_submission(value)
        reference = request.get("credential_ref")
        request = {key: content for key, content in request.items() if key != "credential_ref"}
        # Restrict inline headers so credentials cannot accidentally enter snapshots.
        permitted = {"content-type", "accept", "idempotency-key", "x-correlation-id"}
        if not self.development and set(request["headers"]) - permitted:
            raise InvalidRequest("Use credential_ref for supplier-specific headers")
        _, origin = self.policy.validate(request["url"])
        if reference is None and not self.development:
            raise InvalidRequest("Production requests require an authorized supplier credential_ref")
        request["_retry_mode"] = "manual_on_ambiguous"
        request["_key_window_seconds"] = None
        if reference is not None:
            if not isinstance(reference, str) or not re.fullmatch(r"[\w-]{1,64}", reference):
                raise InvalidRequest("Invalid credential reference")
            contract = self.supplier_contract(reference, principal.tenant, origin)
            if not self._operation_allowed(
                contract, request["method"], urlsplit(request["url"]).path or "/",
                urlsplit(request["url"]).query,
            ):
                raise InvalidRequest("Supplier operation is not authorized")
            secret_headers = contract.get("headers", {})
            self._merged(request, secret_headers)
            request["_retry_mode"] = contract.get("retry_mode", "manual_on_ambiguous")
            if request["_retry_mode"] == "idempotent" and not request["headers"].get("idempotency-key"):
                raise InvalidRequest("Idempotent supplier contract requires a stable Idempotency-Key header")
            if request["_retry_mode"] == "idempotent":
                window = contract.get("deduplication_window_seconds")
                if type(window) is not int or not 60 <= window <= 31536000:
                    raise InvalidRequest("Configure the verified supplier deduplication window (60s to 1 year)")
                request["_key_window_seconds"] = window
            request["_supplier_contract_digest"] = self.supplier_contract_fingerprint(contract)
        if reference is not None:
            request["credential_ref"] = reference
        return request, origin_key_value, self.fingerprint(request)

    def normalize_submission(self, value):
        if not isinstance(value, dict):
            raise InvalidRequest("Expected a JSON object")
        value = dict(value)
        reference = value.pop("credential_ref", None)
        if reference is not None and (
            not isinstance(reference, str) or not re.fullmatch(r"[\w-]{1,64}", reference)
        ):
            raise InvalidRequest("Invalid credential reference")
        request, _ = notification_request(value, self.policy, authorize_target=False)
        if reference is not None:
            request["credential_ref"] = reference
        _, origin = split_target(request["url"])
        return request, origin_key(origin)

    def _merged(self, request, headers):
        if not isinstance(headers, dict):
            raise InvalidRequest("Invalid credential headers")
        normalized = {key.lower() for key in headers}
        if normalized & set(request["headers"]):
            raise InvalidRequest("Credential headers cannot be overridden")
        merged = {**request, "headers": {**request["headers"], **headers}}
        merged.pop("credential_ref", None)
        merged.pop("_retry_mode", None)
        merged.pop("_key_window_seconds", None)
        merged.pop("_supplier_contract_digest", None)
        return notification_request(merged, self.policy)[0]

    def materialize(self, request, tenant):
        request = dict(request)
        reference = request.pop("credential_ref", None)
        frozen_mode = request.pop("_retry_mode", "manual_on_ambiguous")
        frozen_window = request.pop("_key_window_seconds", None)
        frozen_contract_digest = request.pop("_supplier_contract_digest", None)
        if reference:
            _, origin = self.policy.validate(request["url"])
            contract = self.supplier_contract(reference, tenant, origin)
            if not self._operation_allowed(
                contract, request["method"], urlsplit(request["url"]).path or "/",
                urlsplit(request["url"]).query,
            ):
                raise InvalidRequest("Supplier operation is not authorized")
            if contract.get("retry_mode", "manual_on_ambiguous") != frozen_mode:
                raise InvalidRequest("Supplier retry contract changed; use versioned references")
            if frozen_window and contract.get("deduplication_window_seconds", 0) < frozen_window:
                raise InvalidRequest("Supplier deduplication guarantee was shortened")
            if (frozen_contract_digest is not None
                    and not hmac.compare_digest(
                        frozen_contract_digest, self.supplier_contract_fingerprint(contract),
                    )):
                raise InvalidRequest("Supplier account or operation contract changed; use a new reference")
            return self._merged(request, contract.get("headers", {}))
        return request


@dataclass
class Settings:
    dsn: str
    auth_file: str
    keys_file: str
    credentials_file: str
    origins: tuple
    development: bool = False
    workers: int = 8
    per_origin: int = 2
    rate_per_second: float = 5
    max_pending: int = 10000
    max_tenant_pending: int = 1000
    max_retained: int = 100000
    max_snapshot_bytes: int = 1073741824
    max_attempts: int = 24
    delivery_window: int = 86400
    lease_seconds: int = 30
    timeout_seconds: float = 10
    circuit_threshold: int = 5
    circuit_seconds: int = 60
    payload_retention_days: int = 30
    metadata_retention_days: int = 90
    pool_size: int = 16

    def __post_init__(self):
        if not self.dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("Production runtime requires PostgreSQL")
        if not self.development and parse_qs(urlsplit(self.dsn).query).get("sslmode") != ["verify-full"]:
            raise ValueError("Production PostgreSQL requires sslmode=verify-full and trusted CA configuration")
        if not self.origins:
            raise ValueError("An explicit supplier origin allowlist is required")
        bounds = {
            "workers": (1, 64), "per_origin": (1, 32), "rate_per_second": (.1, 1000),
            "max_pending": (1, 1000000), "max_tenant_pending": (1, 100000),
            "max_retained": (1, 10000000), "max_snapshot_bytes": (1048576, 1099511627776),
            "max_attempts": (1, 100), "delivery_window": (60, 604800),
            "lease_seconds": (5, 300), "timeout_seconds": (.1, 30),
            "circuit_threshold": (1, 100), "circuit_seconds": (1, 3600),
            "payload_retention_days": (1, 365), "metadata_retention_days": (1, 3650),
            "pool_size": (2, 128),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
        if self.lease_seconds <= self.timeout_seconds + 3:
            raise ValueError("Lease must exceed HTTP and DNS timeouts")
        if self.per_origin >= self.workers:
            raise ValueError("Per-origin concurrency must leave worker capacity for other suppliers")
        if self.metadata_retention_days < self.payload_retention_days:
            raise ValueError("Metadata retention must cover payload retention")
        policy = TargetPolicy(self.origins, self.development)
        if not self.development and any(origin[0] != "https" for origin in policy.origins):
            raise ValueError("Production suppliers must use HTTPS")

    @classmethod
    def from_env(cls):
        database_url = (
            Path(os.environ["NOTIFY_DATABASE_URL_FILE"]).read_text().strip()
            if "NOTIFY_DATABASE_URL_FILE" in os.environ else os.environ["NOTIFY_DATABASE_URL"]
        )
        kwargs = {
            "dsn": database_url,
            "auth_file": os.environ["NOTIFY_AUTH_FILE"],
            "keys_file": os.environ["NOTIFY_KEYS_FILE"],
            "credentials_file": os.environ["NOTIFY_CREDENTIALS_FILE"],
            "origins": tuple(filter(None, os.environ["NOTIFY_ALLOWED_ORIGINS"].split(","))),
            "development": os.getenv("NOTIFY_ENV", "production") == "development",
        }
        for name, field in cls.__dataclass_fields__.items():
            variable = "NOTIFY_" + name.upper()
            if name not in kwargs and variable in os.environ:
                kwargs[name] = field.type(os.environ[variable])
        return cls(**kwargs)
