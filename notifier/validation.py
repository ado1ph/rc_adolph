import hashlib
import ipaddress
import json
import re
import socket
from urllib.parse import urlsplit


class InvalidRequest(ValueError):
    pass


HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
BLOCKED_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection", "proxy-authorization",
    "proxy-connection", "keep-alive", "te", "trailer", "upgrade", "expect",
}
MAX_ENVELOPE_BYTES = 256 * 1024
MAX_BODY_BYTES = 128 * 1024


def strict_json(data):
    def reject_constant(value):
        raise InvalidRequest("Non-finite JSON numbers are not accepted")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InvalidRequest("Duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(data, parse_constant=reject_constant, object_pairs_hook=unique_keys)
    except (ValueError, UnicodeError) as exc:
        raise InvalidRequest("Invalid JSON: " + str(exc)) from exc


def split_target(url):
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise InvalidRequest("url must be a nonempty string of at most 4096 characters")
    if any(ord(char) <= 32 or ord(char) >= 127 for char in url):
        raise InvalidRequest("url must be an ASCII URL without whitespace or control characters")
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise InvalidRequest("url must be an absolute HTTP(S) URL")
        if parts.username is not None or parts.password is not None or parts.fragment:
            raise InvalidRequest("URL credentials and fragments are not allowed")
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise InvalidRequest("Invalid target port")
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if "%" in host or "\\" in parts.netloc or host.endswith("."):
            raise InvalidRequest("Ambiguous hostname")
        return parts, (parts.scheme, host, port)
    except (ValueError, UnicodeError) as exc:
        raise InvalidRequest("Invalid target URL") from exc


class TargetPolicy:
    """Exact origin allowlist plus DNS/IP checks repeated on every delivery."""

    def __init__(self, origins=(), allow_loopback=False, resolver=None):
        self.origins = set()
        self.allow_loopback = allow_loopback
        self.resolver = resolver
        for origin in origins:
            parts, normalized = split_target(origin.strip())
            if parts.path not in {"", "/"} or parts.query:
                raise InvalidRequest("Allowed origins cannot contain a path or query")
            self.origins.add(normalized)

    def validate(self, url):
        parts, origin = split_target(url)
        if origin not in self.origins:
            raise InvalidRequest("Target origin is not allowlisted")
        if parts.scheme == "http":
            try:
                local = ipaddress.ip_address(origin[1]).is_loopback
            except ValueError:
                local = False
            if not self.allow_loopback or not local:
                raise InvalidRequest("Plain HTTP is only allowed for explicit loopback demos")
        return parts, origin

    def resolve(self, url, timeout=None):
        parts, origin = self.validate(url)
        if self.resolver is not None:
            ips = self.resolver(origin[1], timeout=timeout)
        else:
            if timeout is not None:
                # getaddrinfo has no portable deadline/cancellation API.
                # Production supplies the bounded resolver below.
                pass
            addresses = socket.getaddrinfo(origin[1], origin[2], type=socket.SOCK_STREAM)
            ips = list(dict.fromkeys(item[4][0] for item in addresses))
        if not ips:
            raise InvalidRequest("Target has no addresses")
        for ip in ips:
            address = ipaddress.ip_address(ip)
            # Mapped IPv6 must obey the embedded IPv4 policy as well.
            address = getattr(address, "ipv4_mapped", None) or address
            public = address.is_global and not address.is_multicast and not address.is_reserved
            if not public and not (self.allow_loopback and address.is_loopback):
                raise InvalidRequest("Target resolves to a non-public address")
        return parts, origin, ips[0]


def notification_request(value, policy, authorize_target=True):
    if not isinstance(value, dict) or set(value) - {"url", "method", "headers", "body"}:
        raise InvalidRequest("Expected an object with url, method, headers and body")
    url = value.get("url")
    if authorize_target:
        policy.validate(url)
    else:
        split_target(url)
    method = value.get("method", "POST")
    if not isinstance(method, str) or method not in {"POST", "PUT", "PATCH"}:
        raise InvalidRequest("method must be POST, PUT or PATCH")
    body = value.get("body", "")
    if not isinstance(body, str):
        raise InvalidRequest("body must be a raw UTF-8 string, not a JSON object")
    try:
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise InvalidRequest("body exceeds 128 KiB")
    except UnicodeError as exc:
        raise InvalidRequest("body must be valid UTF-8") from exc
    headers = value.get("headers", {})
    if not isinstance(headers, dict) or len(headers) > 32:
        raise InvalidRequest("headers must be an object with at most 32 entries")
    normalized = {}
    for name, content in headers.items():
        if not isinstance(name, str) or not HEADER_NAME.fullmatch(name):
            raise InvalidRequest("Invalid header name")
        name = name.lower()
        if name in BLOCKED_HEADERS or name in normalized:
            raise InvalidRequest("Forbidden or duplicate header")
        if not isinstance(content, str) or any(
            ord(char) < 32 or ord(char) > 126 for char in content
        ):
            raise InvalidRequest("Header values must be printable ASCII")
        normalized[name] = content
    if sum(len(key) + len(content) for key, content in normalized.items()) > 16384:
        raise InvalidRequest("Headers exceed 16 KiB")
    result = {"url": url, "method": method, "headers": normalized, "body": body}
    digest = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    return result, digest
