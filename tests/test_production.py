import concurrent.futures
import hashlib
import importlib.util
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, urlencode

AVAILABLE = all(importlib.util.find_spec(name) for name in (
    "fastapi", "psycopg", "cryptography", "dns", "prometheus_client", "httpx",
))
DSN = os.getenv("NOTIFY_TEST_DATABASE_URL")
if AVAILABLE:
    import psycopg
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from psycopg import sql

    from notifier.delivery import Result
    from notifier.production.api import create_app
    from notifier.production.config import Principal, Settings
    from notifier.production.runtime import SupplierTransport, bounded_dns, security_for
    from notifier.production.store import CapacityExceeded, PayloadExpired, PostgresStore
    from notifier.store import Conflict
    from notifier.validation import InvalidRequest


@unittest.skipUnless(AVAILABLE, "Install production/test dependencies")
class SecurityFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.token = "a" * 40
        self.auth = self.directory / "auth.json"
        self.keys = self.directory / "keys.json"
        self.credentials = self.directory / "credentials.json"
        self.auth.write_text(json.dumps([
            {
                "name": "client", "tenant": "acme", "role": "sender",
                "token_sha256": hashlib.sha256(self.token.encode()).hexdigest(),
            },
            {
                "name": "ops", "tenant": "acme", "role": "operator",
                "token_sha256": hashlib.sha256(("o" * 40).encode()).hexdigest(),
            },
            {
                "name": "reconciler", "tenant": "acme", "role": "reconciler",
                "token_sha256": hashlib.sha256(("r" * 40).encode()).hexdigest(),
            },
        ]))
        self.keys.write_text(json.dumps({
            "encryption_keys": [Fernet.generate_key().decode()], "fingerprint_key": "ba" * 32,
        }))
        self.credentials.write_text(json.dumps({
            "crm": {"supplier_account": "supplier-main", "tenants": ["acme"],
                    "origins": ["https://supplier.example"],
                    "retry_mode": "duplicate_tolerant",
                    "operations": [{"method": "POST", "path": "/api", "query": {}},
                                   {"method": "POST", "path": "/orders/{order_id}", "query": {}}],
                    "headers": {"X-API-Key": "supplier-secret"}},
            "other": {"supplier_account": "supplier-secondary", "tenants": ["other", "acme"],
                      "origins": ["https://supplier.example", "https://other.example"],
                      "retry_mode": "duplicate_tolerant",
                      "operations": [{"method": "POST", "path": "/api", "query": {}},
                                     {"method": "POST", "path": "/vendor/orders", "query": {}},
                                     {"method": "POST", "path": "/vendor/legacy-orders", "query": {}}],
                      "headers": {}},
        }))
        self.settings = Settings(
            "postgresql://unused:unused@localhost/test?sslmode=verify-full",
            str(self.auth), str(self.keys), str(self.credentials),
            ("https://supplier.example", "https://other.example"),
        )
        self.security = security_for(self.settings)
        self.principal = Principal("client", "acme", "sender")
        self.request = {
            "url": "https://supplier.example/api", "headers": {"Content-Type": "application/json"},
            "body": '{"order":"private-order"}', "credential_ref": "crm",
        }

    def tearDown(self):
        self.temp.cleanup()


@unittest.skipUnless(AVAILABLE, "Install production/test dependencies")
class ProductionSecurityTests(SecurityFixture):
    def test_authentication_and_live_rotation(self):
        self.assertIsNotNone(self.security.authenticate("Bearer " + self.token))
        self.assertIsNone(self.security.authenticate("Bearer " + "x" * 40))
        self.auth.write_text(json.dumps([{
            "name": "client", "tenant": "acme", "role": "sender",
            "token_sha256": hashlib.sha256(b"b" * 40).hexdigest(),
        }]))
        self.assertIsNone(self.security.authenticate("Bearer " + self.token))
        self.assertIsNotNone(self.security.authenticate("Bearer " + "b" * 40))

    def test_credentials_bound_to_tenant_and_origin(self):
        request, _, _ = self.security.prepare(self.request, self.principal)
        wire = self.security.materialize(request, "acme")
        self.assertEqual(wire["headers"]["x-api-key"], "supplier-secret")
        self.assertNotIn("supplier-secret", json.dumps(request))
        with self.assertRaises(InvalidRequest):
            self.security.prepare(self.request, Principal("other", "elsewhere", "sender"))
        with self.assertRaises(InvalidRequest):
            self.security.prepare({**self.request, "url": "https://other.example"}, self.principal)

    def test_inline_credentials_and_override_rejected(self):
        with self.assertRaises(InvalidRequest):
            self.security.prepare({**self.request, "headers": {"Authorization": "secret"}}, self.principal)
        with self.assertRaises(InvalidRequest):
            self.security.prepare({
                **self.request, "url": "https://supplier.example/admin/delete",
            }, self.principal)
        self.security.prepare({
            **self.request, "url": "https://supplier.example/orders/order-123",
        }, self.principal)
        with self.assertRaises(InvalidRequest):
            self.security.prepare({
                **self.request, "url": "https://supplier.example/orders/%2e%2e",
            }, self.principal)
        with self.assertRaises(InvalidRequest):
            self.security._merged({"url": self.request["url"], "headers": {"content-type": "x"},
                                   "method": "POST", "body": ""}, {"Content-Type": "y"})

    def test_supplier_operation_allowlist_checks_method_path_and_query(self):
        with self.assertRaises(InvalidRequest):
            self.security.prepare({**self.request, "method": "PATCH"}, self.principal)
        with self.assertRaises(InvalidRequest):
            self.security.prepare({
                **self.request, "url": "https://supplier.example/api?admin=true",
            }, self.principal)
        with self.assertRaises(InvalidRequest):
            self.security.prepare({
                **self.request, "url": "https://supplier.example/api?mode=create&mode=delete",
            }, self.principal)

        registry = json.loads(self.credentials.read_text())
        registry["crm"]["operations"].append(
            {"method": "POST", "path": "/api",
             "query": {"mode": ["create"], "filter": ["{order_id}"]}},
        )
        self.credentials.write_text(json.dumps(registry))
        self.security = security_for(self.settings)
        self.security.prepare({
            **self.request, "url": "https://supplier.example/api?mode=create&filter=active",
        }, self.principal)
        self.security.prepare({
            **self.request, "url": "https://supplier.example/api?mode=create&filter=order-123",
        }, self.principal)
        with self.assertRaises(InvalidRequest):
            self.security.prepare({
                **self.request, "url": "https://supplier.example/api?mode=delete&filter=active",
            }, self.principal)

    def test_root_operation_is_valid_and_matches_only_root(self):
        registry = json.loads(self.credentials.read_text())
        registry["crm"]["operations"] = [{"method": "POST", "path": "/", "query": {}}]
        self.credentials.write_text(json.dumps(registry))
        self.security = security_for(self.settings)
        self.assertTrue(self.security._operation_allowed(registry["crm"], "POST", "/", ""))
        self.assertFalse(self.security._operation_allowed(
            registry["crm"], "POST", "/child", "",
        ))
        self.security.prepare({**self.request, "url": "https://supplier.example/"}, self.principal)
        registry["crm"]["operations"][0]["query"] = {"mode": ["create"]}
        self.assertTrue(self.security._operation_allowed(
            registry["crm"], "POST", "/", "mode=create",
        ))
        self.assertFalse(self.security._operation_allowed(
            registry["crm"], "POST", "/", "mode=delete",
        ))
        with self.assertRaises(InvalidRequest):
            self.security.prepare({**self.request, "url": "https://supplier.example//"}, self.principal)

    def test_worker_lease_loss_during_materialization_prevents_http_call(self):
        import threading
        from unittest.mock import Mock

        from notifier.production.runtime import SupplierTransport
        from notifier.worker import Worker

        materializing = threading.Event()
        job = {
            "id": "job-1", "lease_token": "token", "origin": "https://supplier.example:443",
            "request": {"snapshot": {"url": self.request["url"]}, "tenant": "acme"},
            "cycle_attempts": 1, "max_attempts": 3,
        }

        class FakeStore:
            def claim(self, _lease):
                return job

            def heartbeat(self, _job, _lease):
                return False

            def finish(self, _job, result, _delay):
                self.result = result
                return True

        store = FakeStore()
        transport = SupplierTransport(self.security, 1)

        def materialize(_snapshot, _tenant):
            materializing.set()
            time.sleep(.04)
            return {"url": self.request["url"], "method": "POST", "headers": {}, "body": ""}

        transport.security.materialize = materialize
        transport.http.send = Mock()
        self.assertTrue(Worker(store, transport, lease_seconds=.03).run_once())
        self.assertTrue(materializing.is_set())
        transport.http.send.assert_not_called()
        self.assertEqual(store.result.error, "lease_lost")

    def test_ambiguous_transport_result_requires_declared_supplier_safety(self):
        from unittest.mock import Mock

        ambiguous = Result(
            error="ambiguous_network_timeout", ambiguous=True,
            failure_class="supplier_network", request_state="possibly_sent",
            failure_phase="response",
        )
        transport = SupplierTransport(self.security, 1)
        transport.http.send = Mock(return_value=ambiguous)

        registry = json.loads(self.credentials.read_text())
        registry["crm"]["retry_mode"] = "manual_on_ambiguous"
        self.credentials.write_text(json.dumps(registry))
        snapshot, _, _ = self.security.prepare(self.request, self.principal)
        result = transport.send({"snapshot": snapshot, "tenant": "acme"})
        self.assertEqual(result.error, "manual_reconciliation_required")
        self.assertFalse(result.retryable)

        registry["crm"]["retry_mode"] = "duplicate_tolerant"
        self.credentials.write_text(json.dumps(registry))
        snapshot, _, _ = self.security.prepare(self.request, self.principal)
        result = transport.send({"snapshot": snapshot, "tenant": "acme"})
        self.assertTrue(result.retryable)
        self.assertTrue(result.ambiguous)

        registry["crm"].update(
            retry_mode="idempotent", deduplication_window_seconds=3600,
        )
        self.credentials.write_text(json.dumps(registry))
        request = {**self.request, "headers": {
            "Content-Type": "application/json", "Idempotency-Key": "stable-vendor-key",
        }}
        snapshot, _, _ = self.security.prepare(request, self.principal)
        result = transport.send({"snapshot": snapshot, "tenant": "acme"})
        self.assertTrue(result.retryable)
        self.assertEqual(
            snapshot["headers"]["idempotency-key"],
            "stable-vendor-key",
        )

    def test_submission_fingerprint_is_stable_without_current_origin_authorization(self):
        normalized, _ = self.security.normalize_submission(self.request)
        normalized_digest = self.security.fingerprint(normalized)
        _, _, prepared_digest = self.security.prepare(self.request, self.principal)
        self.assertEqual(normalized_digest, prepared_digest)

        self.security.policy.origins.clear()
        repeated, _ = self.security.normalize_submission(self.request)
        self.assertEqual(self.security.fingerprint(repeated), normalized_digest)
        with self.assertRaises(InvalidRequest):
            self.security.prepare(self.request, self.principal)

    def test_oversized_retry_after_hold_survives_manual_retry_policy(self):
        registry = json.loads(self.credentials.read_text())
        registry["crm"]["retry_mode"] = "manual_on_ambiguous"
        self.credentials.write_text(json.dumps(registry))
        from unittest.mock import Mock

        from notifier.production.runtime import SupplierTransport

        held = Result(
            retryable=True, http_status=429,
            error="retry_after_exceeds_supported_range",
            cooldown_hold=True, cooldown_reason="retry_after_exceeds_supported_range",
            failure_class="supplier_service",
        )
        transport = SupplierTransport(self.security, 1)
        transport.http.send = Mock(return_value=held)
        snapshot, _, _ = self.security.prepare(self.request, self.principal)
        result = transport.send({"snapshot": snapshot, "tenant": "acme"})
        self.assertTrue(result.cooldown_hold)
        self.assertEqual(result.error, "manual_reconciliation_required")
        self.assertEqual(result.cooldown_reason, "retry_after_exceeds_supported_range")

    def test_encryption_rotation_preserves_fingerprint(self):
        request, _, digest = self.security.prepare(self.request, self.principal)
        encrypted = self.security.encode(request)
        self.assertNotIn("private-order", encrypted)
        keys = json.loads(self.keys.read_text())
        keys["encryption_keys"].insert(0, Fernet.generate_key().decode())
        self.keys.write_text(json.dumps(keys))
        self.assertEqual(self.security.decode(encrypted), request)
        self.assertEqual(self.security.fingerprint(request), digest)

    def test_fail_closed_settings(self):
        from dataclasses import replace
        for kwargs in ({"dsn": "sqlite:///demo"}, {"origins": ()},
                       {"origins": ("http://127.0.0.1:9000",)},
                       {"lease_seconds": 5}, {"rate_per_second": 0}):
            with self.assertRaises(ValueError):
                replace(self.settings, **kwargs)

    def test_dns_deadline_and_mixed_private_addresses(self):
        import dns.exception
        with patch("notifier.production.runtime.dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve.side_effect = dns.exception.Timeout()
            with self.assertRaises(OSError):
                bounded_dns("supplier.example", .01)
        self.security.policy.resolver = lambda host, timeout=None: ["8.8.8.8", "127.0.0.1"]
        with self.assertRaises(InvalidRequest):
            self.security.policy.resolve(self.request["url"])
        self.assertEqual(bounded_dns("8.8.8.8"), ["8.8.8.8"])

    def test_credential_configuration_failure_is_sanitized(self):
        transport = SupplierTransport(self.security, 1)
        result = transport.send({"snapshot": {"credential_ref": "missing", "url": self.request["url"]},
                                 "tenant": "acme"})
        self.assertFalse(result.retryable)
        self.assertEqual(result.failure_class, "local_authorization")
        transport.security.credentials_file = str(self.directory / "missing-credentials.json")
        result = transport.send({
            "snapshot": {"credential_ref": "crm", "url": self.request["url"]}, "tenant": "acme",
        })
        self.assertTrue(result.retryable)
        self.assertEqual(result.error, "credential_configuration_unavailable")
        self.assertEqual(result.failure_class, "local_configuration")

    def test_unsafe_supplier_result_requires_reconciliation(self):
        registry = json.loads(self.credentials.read_text())
        registry["crm"]["retry_mode"] = "manual_on_ambiguous"
        self.credentials.write_text(json.dumps(registry))
        request, _, _ = self.security.prepare(self.request, self.principal)
        request["_retry_mode"] = "manual_on_ambiguous"
        transport = SupplierTransport(self.security, 1)
        envelope = {"snapshot": request, "tenant": "acme"}
        with patch.object(transport.http, "send", return_value=Result(
            retryable=True, ambiguous=True, error="network_error",
        )):
            result = transport.send(envelope)
            self.assertFalse(result.retryable)
            self.assertEqual(result.error, "manual_reconciliation_required")
        with patch.object(transport.http, "send", return_value=Result(retryable=True, http_status=503)):
            self.assertFalse(transport.send(envelope).retryable)
        with patch.object(transport.http, "send", return_value=Result(retryable=True, error="network_error")):
            self.assertTrue(transport.send(envelope).retryable)  # DNS failure before sending is safe.

    def test_idempotent_contract_requires_key_and_verified_window(self):
        registry = json.loads(self.credentials.read_text())
        registry["crm"]["retry_mode"] = "idempotent"
        self.credentials.write_text(json.dumps(registry))
        with self.assertRaises(InvalidRequest):
            self.security.prepare(self.request, self.principal)
        request = {**self.request, "headers": {"Idempotency-Key": "supplier-operation-123"}}
        with self.assertRaises(InvalidRequest):
            self.security.prepare(request, self.principal)
        registry["crm"]["deduplication_window_seconds"] = 86400
        self.credentials.write_text(json.dumps(registry))
        snapshot, _, _ = self.security.prepare(request, self.principal)
        self.assertEqual(snapshot["_key_window_seconds"], 86400)
        self.assertNotIn("_key_window_seconds", self.security.materialize(snapshot, "acme"))

    def test_contract_change_cannot_silently_weaken_existing_retry_safety(self):
        snapshot, _, _ = self.security.prepare(self.request, self.principal)
        registry = json.loads(self.credentials.read_text())
        registry["crm"]["retry_mode"] = "manual_on_ambiguous"
        self.credentials.write_text(json.dumps(registry))
        with self.assertRaises(InvalidRequest):
            self.security.materialize(snapshot, "acme")
        # A caller retry still points at the original accepted job, not a new policy snapshot.
        changed, _, digest = self.security.prepare(self.request, self.principal)
        self.assertNotEqual(changed["_retry_mode"], snapshot["_retry_mode"])
        self.assertEqual(digest, self.security.fingerprint(snapshot))

    def test_queued_request_pins_account_and_operation_contract_but_allows_key_rotation(self):
        snapshot, _, _ = self.security.prepare(self.request, self.principal)
        registry = json.loads(self.credentials.read_text())
        registry["crm"]["headers"]["X-API-Key"] = "rotated-key-same-account"
        self.credentials.write_text(json.dumps(registry))
        wire = self.security.materialize(snapshot, "acme")
        self.assertEqual(wire["headers"]["x-api-key"], "rotated-key-same-account")
        self.assertNotIn("_supplier_contract_digest", wire)

        registry["crm"]["supplier_account"] = "supplier-different-account"
        self.credentials.write_text(json.dumps(registry))
        with self.assertRaises(InvalidRequest):
            self.security.materialize(snapshot, "acme")

        registry["crm"]["supplier_account"] = "supplier-main"
        registry["crm"]["operations"].append(
            {"method": "POST", "path": "/admin/delete", "query": {}},
        )
        self.credentials.write_text(json.dumps(registry))
        with self.assertRaises(InvalidRequest):
            self.security.materialize(snapshot, "acme")

    def test_real_tls_validates_hostname_with_pinned_socket(self):
        import ssl
        from datetime import datetime, timedelta, timezone

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        from notifier.delivery import HttpTransport
        from notifier.mock_supplier import supplier_server
        from notifier.validation import TargetPolicy

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "supplier.test")])
        now = datetime.now(timezone.utc)
        certificate = (
            x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("supplier.test")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        cert_file, key_file = self.directory / "ca.pem", self.directory / "tls.key"
        cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
        sni = []
        context.set_servername_callback(lambda sock, name, ctx: sni.append(name))
        supplier = supplier_server(port=0)
        supplier.server.socket = context.wrap_socket(supplier.server.socket, server_side=True)
        supplier.start()
        port = supplier.server.server_address[1]
        # Loopback is enabled only for this isolated test. Production rejects private IPs.
        policy = TargetPolicy([f"https://supplier.test:{port}", f"https://wrong.test:{port}"],
                              allow_loopback=True,
                              resolver=lambda host, timeout=None: ["127.0.0.1"])
        request = {
            "url": f"https://supplier.test:{port}/vendor/orders", "method": "POST",
            "headers": {"X-API-Key": "supplier-demo-secret", "Content-Type": "application/json"},
            "body": '{"order_id":"tls-test","amount_cents":100}',
        }
        try:
            with patch.dict(os.environ, {"SSL_CERT_FILE": str(cert_file)}):
                result = HttpTransport(policy, 2).send(request)
                self.assertTrue(result.success, result)
                wrong = HttpTransport(policy, 2).send({
                    **request, "url": f"https://wrong.test:{port}/vendor/orders",
                })
                self.assertEqual(wrong.error, "tls_certificate_invalid")
                self.assertFalse(wrong.retryable)
            self.assertIn("supplier.test", sni)
            self.assertEqual(supplier.state.snapshot()["effects"]["tls-test"], 1)
        finally:
            supplier.stop()


@unittest.skipUnless(AVAILABLE and DSN, "Requires NOTIFY_TEST_DATABASE_URL (isolated PostgreSQL)")
class ProductionDatabaseTests(SecurityFixture):
    def setUp(self):
        super().setUp()
        self.schema = "test_" + uuid.uuid4().hex
        with psycopg.connect(DSN) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.settings.dsn = DSN + ("&" if "?" in DSN else "?") + urlencode({
            "options": "-c search_path=" + self.schema,
        }, quote_via=quote)
        self.settings.development = True
        self.settings.rate_per_second = 1000
        PostgresStore.migrate(self.settings.dsn)
        self.store = PostgresStore(self.settings, self.security)
        self.store.open()
        self.client = TestClient(create_app(self.settings, self.store, self.security))
        self.client.__enter__()
        self.headers = {"Authorization": "Bearer " + self.token, "Idempotency-Key": "test-key"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.store.close()
        with psycopg.connect(DSN) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
        super().tearDown()

    def submit_job(self, key=None, principal=None, request=None):
        principal = principal or self.principal
        request, origin, digest = self.security.prepare(request or self.request, principal)
        return self.store.submit(principal, key or uuid.uuid4().hex, request, origin, digest)[0]

    def force_expire(self, identifier):
        with self.store.pool.connection() as conn:
            conn.execute("UPDATE delivery_jobs SET lease_until=0 WHERE id=%s", (identifier,))

    def test_version_one_schema_migrates_and_quarantines_legacy_ambiguous_jobs(self):
        schema = "upgrade_" + uuid.uuid4().hex
        legacy_identifier = uuid.uuid4()
        delivered_identifier = uuid.uuid4()
        held_identifier = uuid.uuid4()
        ambiguous_identifier = uuid.uuid4()
        response_identifier = uuid.uuid4()
        with psycopg.connect(DSN) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped_dsn = DSN + ("&" if "?" in DSN else "?") + urlencode(
            {"options": "-c search_path=" + schema}, quote_via=quote,
        )
        try:
            with psycopg.connect(scoped_dsn) as conn:
                conn.execute((Path(__file__).resolve().parents[1]
                              / "notifier/production/schema.sql").read_text())
                self.assertEqual(conn.execute("SELECT version FROM delivery_schema").fetchone()[0], 1)
                conn.execute("INSERT INTO delivery_origins(origin) VALUES ('https://supplier.example:443')")
                conn.execute(
                    """INSERT INTO delivery_jobs
                    (id,tenant,idempotency_key,request_hash,origin,retry_mode,status,
                     attempt_count,cycle_attempts,max_attempts,next_attempt_at,deadline,
                     last_error,created_at,updated_at)
                    VALUES (%s,'acme','legacy-key','legacy-hash','https://supplier.example:443',
                            'manual_on_ambiguous','failed',1,1,3,1,100,
                            'manual_reconciliation_required',1,1)""",
                    (legacy_identifier,),
                )
                conn.execute(
                    """INSERT INTO delivery_attempts(notification_id,number,outcome,started_at,
                    finished_at,error) VALUES (%s,1,'permanent_failure',1,2,
                    'manual_reconciliation_required')""", (legacy_identifier,),
                )
                conn.execute(
                    """INSERT INTO delivery_jobs
                    (id,tenant,idempotency_key,request_hash,origin,retry_mode,status,
                     attempt_count,cycle_attempts,max_attempts,next_attempt_at,deadline,
                     last_error,created_at,updated_at)
                    VALUES (%s,'acme','uncertain-key','uncertain-hash',
                            'https://supplier.example:443','idempotent','failed',
                            2,2,3,1,100,'credential_authorization_rejected',1,2)""",
                    (ambiguous_identifier,),
                )
                conn.execute(
                    """INSERT INTO delivery_attempts(notification_id,number,outcome,started_at,
                    finished_at,error) VALUES (%s,1,'retryable_failure',1,1.5,
                    'ambiguous_network_timeout'),(%s,2,'permanent_failure',1.5,2,
                    'credential_authorization_rejected')""",
                    (ambiguous_identifier, ambiguous_identifier),
                )
                conn.execute(
                    """INSERT INTO delivery_jobs
                    (id,tenant,idempotency_key,request_hash,origin,retry_mode,status,
                     attempt_count,cycle_attempts,max_attempts,next_attempt_at,deadline,
                     last_error,created_at,updated_at)
                    VALUES (%s,'acme','response-key','response-hash',
                            'https://supplier.example:443','duplicate_tolerant','failed',
                            1,1,3,1,100,'retry_exhausted',1,2)""",
                    (response_identifier,),
                )
                conn.execute(
                    """INSERT INTO delivery_attempts(notification_id,number,outcome,started_at,
                    finished_at,http_status,error)
                    VALUES (%s,1,'retry_exhausted',1,2,503,'http_503')""",
                    (response_identifier,),
                )
                conn.execute(
                    """INSERT INTO delivery_jobs
                    (id,tenant,idempotency_key,request_hash,origin,retry_mode,status,
                     attempt_count,cycle_attempts,max_attempts,next_attempt_at,deadline,
                     last_error,created_at,updated_at)
                    VALUES (%s,'acme','delivered-key','delivered-hash',
                            'https://supplier.example:443','manual_on_ambiguous','failed',
                            1,1,3,1,100,'manual_reconciliation_required',1,1)""",
                    (delivered_identifier,),
                )
                conn.execute(
                    """INSERT INTO delivery_jobs
                    (id,tenant,idempotency_key,request_hash,origin,retry_mode,status,
                     attempt_count,cycle_attempts,max_attempts,next_attempt_at,deadline,
                     created_at,updated_at)
                    VALUES (%s,'acme','held-key','held-hash','https://supplier.example:443',
                            'duplicate_tolerant','pending',0,0,3,'Infinity'::float8,
                            extract(epoch FROM clock_timestamp())+3600,1,1)""",
                    (held_identifier,),
                )
                conn.execute(
                    "UPDATE delivery_origins SET next_allowed='Infinity'::float8",
                )
                conn.execute("UPDATE delivery_capacity SET retained_jobs=3")
            PostgresStore.migrate(scoped_dsn)
            with psycopg.connect(scoped_dsn) as conn:
                self.assertEqual(conn.execute("SELECT version FROM delivery_schema").fetchone()[0], 7)
                self.assertEqual(conn.execute(
                    "SELECT status FROM delivery_jobs WHERE id=%s", (legacy_identifier,),
                ).fetchone()[0], "reconciliation_required")
                self.assertEqual(conn.execute(
                    "SELECT status,outcome_uncertain FROM delivery_jobs WHERE id=%s",
                    (ambiguous_identifier,),
                ).fetchone(), ("reconciliation_required", True))
                self.assertEqual(conn.execute(
                    "SELECT action FROM delivery_audit WHERE notification_id=%s",
                    (ambiguous_identifier,),
                ).fetchone()[0], "historical_outcome_uncertainty_preserved")
                self.assertEqual(conn.execute(
                    "SELECT status,outcome_uncertain FROM delivery_jobs WHERE id=%s",
                    (response_identifier,),
                ).fetchone(), ("reconciliation_required", True))
                self.assertEqual(conn.execute(
                    "SELECT outcome FROM delivery_attempts WHERE notification_id=%s",
                    (legacy_identifier,),
                ).fetchone()[0], "reconciliation_required")
                self.assertEqual(conn.execute(
                    "SELECT action FROM delivery_audit WHERE notification_id=%s",
                    (legacy_identifier,),
                ).fetchone()[0], "migration_reconciliation_required")
                columns = {
                    row[0] for row in conn.execute(
                        """SELECT column_name FROM information_schema.columns
                        WHERE table_schema=%s AND table_name='delivery_jobs'""", (schema,),
                    )
                }
                self.assertIn("key_window_seconds", columns)
                self.assertIn("outcome_uncertain", columns)
                self.assertTrue(conn.execute(
                    "SELECT outcome_uncertain FROM delivery_jobs WHERE id=%s",
                    (legacy_identifier,),
                ).fetchone()[0])
                self.assertEqual(conn.execute(
                    "SELECT last_tenant FROM delivery_origins",
                ).fetchall(), [("",)])
                held = conn.execute(
                    """SELECT status,next_attempt_at,last_error FROM delivery_jobs WHERE id=%s""",
                    (held_identifier,),
                ).fetchone()
                self.assertEqual(held[0], "paused")
                self.assertTrue(math.isfinite(held[1]))
                self.assertEqual(held[2], "legacy_retry_after_hold")
                origin = conn.execute(
                    "SELECT cooldown_hold,next_allowed FROM delivery_origins",
                ).fetchone()
                self.assertTrue(origin[0])
                self.assertTrue(math.isfinite(origin[1]))
                self.assertEqual(conn.execute(
                    """SELECT action FROM delivery_audit WHERE notification_id=%s""",
                    (held_identifier,),
                ).fetchone()[0], "non_finite_schedule_repaired")
            from dataclasses import replace

            isolated_settings = replace(
                self.settings, dsn=scoped_dsn, development=True,
            )
            upgraded_store = PostgresStore(isolated_settings, self.security)
            upgraded_store.open()
            try:
                reconciler = Principal("reconciler", "acme", "reconciler")
                no_snapshot = upgraded_store.reconcile(
                    reconciler, legacy_identifier, "not_delivered",
                    "INC-LEGACY-1", "vendor confirms no delivery",
                )
                self.assertEqual(no_snapshot["status"], "failed")
                self.assertEqual(
                    no_snapshot["next_action"],
                    "submit_new_notification_with_fresh_supplier_key",
                )
                self.assertIsNone(upgraded_store.claim(30))
                self.assertEqual(
                    upgraded_store.get("acme", legacy_identifier)["reconciliations"][0]["outcome"],
                    "not_delivered",
                )
                delivered = upgraded_store.reconcile(
                    reconciler, delivered_identifier, "delivered",
                    "INC-LEGACY-2", "vendor confirms delivery",
                )
                self.assertEqual(delivered["status"], "succeeded")
            finally:
                upgraded_store.close()
        finally:
            with psycopg.connect(DSN) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    def test_real_postgres_schema_idempotency_and_encryption(self):
        identifier = self.submit_job("fixed")
        self.assertEqual(self.submit_job("fixed"), identifier)
        with self.assertRaises(Conflict):
            self.submit_job("fixed", request={**self.request, "body": "different"})
        with self.store.pool.connection() as conn:
            row = conn.execute("SELECT snapshot FROM delivery_jobs").fetchone()
            self.assertNotIn("private-order", row["snapshot"])
            self.assertNotIn("supplier-secret", row["snapshot"])
        self.assertTrue(self.store.ready())

    def test_idempotent_retry_requires_key_window_for_full_next_send_budget(self):
        registry = json.loads(self.credentials.read_text())
        registry["crm"].update(
            retry_mode="idempotent", deduplication_window_seconds=3600,
        )
        self.credentials.write_text(json.dumps(registry))
        self.security = security_for(self.settings)
        self.store.security = self.security
        request = {**self.request, "headers": {
            "Content-Type": "application/json", "Idempotency-Key": "stable-window-key",
        }}
        identifier = self.submit_job(request=request)
        job = self.store.claim(30)
        self.assertEqual(job["id"], identifier)
        with self.store.pool.connection() as conn:
            conn.execute(
                "UPDATE delivery_jobs SET key_expires_at=%s WHERE id=%s",
                (self.store.now(conn) + self.settings.timeout_seconds + 2, identifier),
            )
        result = Result(
            retryable=True, ambiguous=True, request_state="possibly_sent",
            error="ambiguous_network_timeout", failure_class="supplier_network",
        )
        self.assertTrue(self.store.finish(job, result, 0))
        self.assertEqual(self.store.get("acme", identifier)["status"], "reconciliation_required")
        self.assertIsNone(self.store.claim(30))

    def test_pre_send_network_timeout_retries_but_local_budget_failure_does_not(self):
        identifier = self.submit_job()
        job = self.store.claim(30)
        pre_send_timeout = Result(
            retryable=True, error="network_timeout", request_state="not_sent",
            failure_phase="connect", failure_class="supplier_network",
        )
        self.assertTrue(self.store.finish(job, pre_send_timeout, 0))
        self.assertEqual(self.store.get("acme", identifier)["status"], "pending")
        with self.store.pool.connection() as conn:
            failures = conn.execute(
                "SELECT failures FROM delivery_origins WHERE origin=%s",
                (job["origin"],),
            ).fetchone()["failures"]
            conn.execute(
                "UPDATE delivery_origins SET next_allowed=0 WHERE origin=%s",
                (job["origin"],),
            )
        self.assertEqual(failures, 1)

        retry = self.store.claim(30)
        self.assertEqual(retry["id"], identifier)
        local_budget_failure = Result(
            error="execution_budget_exhausted", failure_class="local_policy",
            request_state="not_sent",
        )
        self.assertTrue(self.store.finish(retry, local_budget_failure, 0))
        self.assertEqual(self.store.get("acme", identifier)["status"], "failed")
        with self.store.pool.connection() as conn:
            self.assertEqual(conn.execute(
                "SELECT failures FROM delivery_origins WHERE origin=%s",
                (job["origin"],),
            ).fetchone()["failures"], 1)

    def test_ambiguous_manual_timeout_requires_reconciliation_not_retry(self):
        identifier = self.submit_job()
        job = self.store.claim(30)
        ambiguous = Result(
            error="manual_reconciliation_required", ambiguous=True, retryable=False,
            request_state="possibly_sent", failure_phase="response",
            failure_class="supplier_network",
        )
        self.assertTrue(self.store.finish(job, ambiguous, 0))
        self.assertEqual(
            self.store.get("acme", identifier)["status"], "reconciliation_required",
        )
        self.assertIsNone(self.store.claim(30))

    def test_expired_lease_recovery_fences_old_success_and_reuses_supplier_key(self):
        registry = json.loads(self.credentials.read_text())
        registry["crm"].update(
            retry_mode="idempotent", deduplication_window_seconds=3600,
        )
        self.credentials.write_text(json.dumps(registry))
        self.security = security_for(self.settings)
        self.store.security = self.security
        request = {**self.request, "headers": {
            "Content-Type": "application/json", "Idempotency-Key": "stable-recovery-key",
        }}
        identifier = self.submit_job(request=request)
        old = self.store.claim(30)
        stable_key = old["request"]["snapshot"]["headers"]["idempotency-key"]
        self.force_expire(identifier)
        new = self.store.claim(30)
        self.assertEqual(new["id"], identifier)
        self.assertEqual(new["request"]["snapshot"]["headers"]["idempotency-key"], stable_key)
        self.assertFalse(self.store.finish(old, Result(success=True), 0))
        self.assertTrue(self.store.finish(new, Result(success=True), 0))
        item = self.store.get("acme", identifier)
        self.assertEqual(item["status"], "succeeded")
        self.assertFalse(item["outcome_uncertain"])
        self.assertEqual([attempt["outcome"] for attempt in item["attempts"]],
                         ["abandoned", "succeeded"])

    def test_two_pool_concurrent_admission_and_claims(self):
        other = PostgresStore(self.settings, self.security)
        other.open()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                identifiers = list(executor.map(lambda _: self.submit_job("same"), range(12)))
                self.assertEqual(len(set(identifiers)), 1)
                for _ in range(5):
                    self.submit_job()
                stores = [self.store, other] * 4
                jobs = list(executor.map(lambda store: store.claim(30), stores))
            jobs = [job for job in jobs if job]
            self.assertEqual(len({job["id"] for job in jobs}), len(jobs))
            self.assertGreaterEqual(len(jobs), 1)
            self.assertLessEqual(len(jobs), self.settings.per_origin)
        finally:
            other.close()

    def test_bulkhead_fairness_rate_and_circuit(self):
        self.settings.per_origin = 1
        first = self.submit_job()
        self.submit_job()
        second = self.submit_job(request={**self.request, "credential_ref": "other",
                                         "url": "https://other.example/api"})
        job = self.store.claim(30)
        independent = self.store.claim(30)
        self.assertEqual({job["id"], independent["id"]}, {first, second})
        self.assertIsNone(self.store.claim(30))
        self.settings.circuit_threshold = 1
        self.store.finish(job, Result(retryable=True, error="http_503", http_status=503), 0)
        self.assertIsNone(self.store.claim(30))
        with self.store.pool.connection() as conn:
            conn.execute("UPDATE delivery_origins SET open_until=0,next_allowed=0")
        probe = self.store.claim(30)
        self.assertIsNotNone(probe)
        self.store.finish(probe, Result(success=True, http_status=204), 0)
        self.assertEqual(self.store.stats()["open_circuits"], 0)

    def test_round_robin_fairness_between_tenants_on_shared_origin(self):
        for _ in range(5):
            self.submit_job(principal=Principal("acme-client", "acme", "sender"))
        other = Principal("other-client", "other", "sender")
        self.submit_job(principal=other, request={**self.request, "credential_ref": "other"})
        order = []
        for _ in range(6):
            with self.store.pool.connection() as conn:
                conn.execute("UPDATE delivery_origins SET next_allowed=0")
            job = self.store.claim(30)
            order.append(job["tenant"])
            self.store.finish(job, Result(success=True, http_status=204), 0)
        self.assertEqual(order, ["acme", "other", "acme", "acme", "acme", "acme"])

    def test_retry_after_is_shared_across_jobs(self):
        self.submit_job()
        self.submit_job()
        job = self.store.claim(30)
        self.store.finish(job, Result(retryable=True, http_status=429, retry_after=60), 60)
        self.assertIsNone(self.store.claim(30))

    def test_unbounded_retry_after_is_paused_and_public_api_returns_finite_json(self):
        first = self.submit_job()
        self.submit_job()
        job = self.store.claim(30)
        self.store.finish(job, Result(
            retryable=True, http_status=429, error="retry_after_exceeds_supported_range",
            cooldown_hold=True, failure_class="supplier_service",
        ), 0)
        detail = self.client.get("/v1/notifications/" + first, headers=self.headers)
        listing = self.client.get("/v1/notifications", headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(listing.status_code, 200, listing.text)
        json.loads(detail.content, parse_constant=lambda value: self.fail(f"invalid JSON number: {value}"))
        json.loads(listing.content, parse_constant=lambda value: self.fail(f"invalid JSON number: {value}"))
        self.assertEqual(detail.json()["status"], "paused")
        with self.store.pool.connection() as conn:
            self.assertEqual(
                conn.execute("SELECT cooldown_hold FROM delivery_origins").fetchone()["cooldown_hold"],
                True,
            )
        self.assertIsNone(self.store.claim(30))
        self.assertEqual(self.store.stats()["retry_after_holds"], 1)
        with self.store.pool.connection() as conn:
            now = self.store.now(conn)
            conn.execute(
                """UPDATE delivery_origins SET cooldown_hold=false,cooldown_reason=NULL,
                next_allowed=%s WHERE origin=%s""",
                (now, "https://supplier.example:443"),
            )
            conn.execute(
                """UPDATE delivery_jobs SET status='pending',next_attempt_at=%s,
                last_error='retry_after_hold_released',updated_at=%s
                WHERE origin=%s AND status='paused' AND deadline>%s+%s
                AND retry_mode IN ('idempotent','duplicate_tolerant')
                AND (retry_mode<>'idempotent' OR
                     (attempt_count=0 AND key_expires_at IS NULL) OR
                     (key_expires_at IS NOT NULL AND key_expires_at>%s+%s))""",
                (now, now, "https://supplier.example:443", now,
                 self.settings.timeout_seconds + 3, now, self.settings.timeout_seconds + 3),
            )
            self.assertEqual(conn.execute(
                """SELECT count(*) FROM delivery_audit
                WHERE action='retry_after_hold' AND notification_id=%s""",
                (first,),
            ).fetchone()["count"], 1)
        self.assertIsNotNone(self.store.claim(30))

    def test_v4_upgrade_quarantines_manual_paused_tasks_but_preserves_safe_pauses(self):
        schema = "upgrade_v4_" + uuid.uuid4().hex
        manual_identifier, safe_identifier = uuid.uuid4(), uuid.uuid4()
        with psycopg.connect(DSN) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped_dsn = DSN + ("&" if "?" in DSN else "?") + urlencode(
            {"options": "-c search_path=" + schema}, quote_via=quote,
        )
        migrations = Path(__file__).resolve().parents[1] / "notifier/production/migrations"
        try:
            with psycopg.connect(scoped_dsn) as conn:
                conn.execute((Path(__file__).resolve().parents[1]
                              / "notifier/production/schema.sql").read_text())
                for version in (2, 3, 4):
                    migration = next(migrations.glob(f"{version:04}_*.sql"))
                    conn.execute(migration.read_text())
                conn.execute("INSERT INTO delivery_origins(origin,cooldown_hold) VALUES (%s,true)",
                             ("https://supplier.example:443",))
                for identifier, mode in (
                    (manual_identifier, "manual_on_ambiguous"),
                    (safe_identifier, "idempotent"),
                ):
                    conn.execute(
                        """INSERT INTO delivery_jobs
                        (id,tenant,idempotency_key,request_hash,origin,retry_mode,status,
                         max_attempts,next_attempt_at,deadline,last_error,created_at,updated_at)
                        VALUES (%s,'acme',%s,%s,'https://supplier.example:443',%s,'paused',
                                5,1,100,'legacy_retry_after_hold',1,1)""",
                        (identifier, str(identifier), str(identifier), mode),
                    )
                conn.execute("UPDATE delivery_capacity SET retained_jobs=2")
            PostgresStore.migrate(scoped_dsn)
            with psycopg.connect(scoped_dsn) as conn:
                rows = conn.execute(
                    "SELECT id,status,last_error FROM delivery_jobs ORDER BY id",
                ).fetchall()
                states = {row[0]: (row[1], row[2]) for row in rows}
                self.assertEqual(
                    states[manual_identifier],
                    ("reconciliation_required", "legacy_paused_manual_outcome_unknown"),
                )
                self.assertEqual(
                    states[safe_identifier], ("paused", "legacy_retry_after_hold"),
                )
                self.assertTrue(conn.execute(
                    "SELECT cooldown_hold FROM delivery_origins",
                ).fetchone()[0])
                self.assertEqual(conn.execute(
                    """SELECT count(*) AS count FROM delivery_audit
                    WHERE action='legacy_paused_manual_outcome_quarantined'""",
                ).fetchone()[0], 1)
                self.assertEqual(conn.execute(
                    "SELECT version FROM delivery_schema",
                ).fetchone()[0], 7)
        finally:
            with psycopg.connect(DSN) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    def test_uncertainty_candidate_query_is_read_only_and_conservative(self):
        schema = "upgrade_v6_" + uuid.uuid4().hex
        names = (
            "success", "overlap_success", "inverted_success", "equal_start_success",
            "equal_finish_success", "delivered", "not_delivered", "reopened",
            "unresolved", "tie",
            "missing_time", "multiple_reconciliations", "retry_pending",
            "retry_inflight", "paused",
        )
        identifiers = {name: uuid.uuid4() for name in names}
        with psycopg.connect(DSN) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped_dsn = DSN + ("&" if "?" in DSN else "?") + urlencode(
            {"options": "-c search_path=" + schema}, quote_via=quote,
        )
        migrations = Path(__file__).resolve().parents[1] / "notifier/production/migrations"
        try:
            with psycopg.connect(scoped_dsn) as conn:
                conn.execute((Path(__file__).resolve().parents[1]
                              / "notifier/production/schema.sql").read_text())
                for version in range(2, 6):
                    conn.execute(next(migrations.glob(f"{version:04}_*.sql")).read_text())
                conn.execute("INSERT INTO delivery_origins(origin) VALUES (%s)",
                             ("https://supplier.example:443",))
                for name, identifier in identifiers.items():
                    status = {
                        "success": "succeeded",
                        "overlap_success": "succeeded",
                        "inverted_success": "succeeded",
                        "equal_start_success": "succeeded",
                        "equal_finish_success": "succeeded",
                        "delivered": "succeeded",
                        "multiple_reconciliations": "succeeded",
                        "reopened": "reconciliation_required",
                        "retry_pending": "pending",
                        "retry_inflight": "in_flight",
                        "paused": "paused",
                    }.get(name, "failed")
                    attempts = 2 if name in {
                        "success", "overlap_success", "inverted_success",
                        "equal_start_success", "equal_finish_success", "reopened",
                        "retry_inflight",
                    } else 1
                    replay_count = 1 if name == "reopened" else 0
                    conn.execute(
                        """INSERT INTO delivery_jobs
                        (id,tenant,idempotency_key,request_hash,origin,retry_mode,status,
                         attempt_count,cycle_attempts,max_attempts,replay_count,next_attempt_at,deadline,
                         lease_token,lease_until,created_at,updated_at)
                        VALUES (%s,'acme',%s,%s,'https://supplier.example:443',
                                'idempotent',%s,%s,%s,4,%s,1,9999999999,
                                %s,%s,1,2)""",
                        (
                            identifier, name, name, status, attempts, attempts, replay_count,
                            "lease-token" if name == "retry_inflight" else None,
                            9999999999 if name == "retry_inflight" else None,
                        ),
                    )
                    conn.execute(
                        """INSERT INTO delivery_attempts(notification_id,number,outcome,
                        http_status,error,started_at,finished_at)
                        VALUES (%s,1,%s,%s,%s,10,%s)""",
                        (
                            identifier,
                            "abandoned" if name == "missing_time" else "retryable_failure",
                            None if name == "missing_time" else 503,
                            "worker_lease_expired" if name == "missing_time" else "http_503",
                            None if name == "missing_time" else 11,
                        ),
                    )
                conn.execute(
                    """INSERT INTO delivery_attempts(notification_id,number,outcome,
                    http_status,started_at,finished_at)
                    VALUES (%s,2,'succeeded',200,20,21)""", (identifiers["success"],),
                )
                conn.execute(
                    """INSERT INTO delivery_attempts(notification_id,number,outcome,
                    http_status,started_at,finished_at)
                    VALUES (%s,2,'succeeded',200,10.5,12),
                           (%s,2,'succeeded',200,20,19),
                           (%s,2,'succeeded',200,11,12),
                           (%s,2,'succeeded',200,20,20)""",
                    (identifiers["overlap_success"], identifiers["inverted_success"],
                     identifiers["equal_start_success"], identifiers["equal_finish_success"]),
                )
                conn.execute(
                    """INSERT INTO delivery_reconciliations
                    (notification_id,tenant,actor,outcome,evidence_ref,reason,created_at)
                    VALUES (%s,'acme','reconciler','delivered','INC-D','confirmed delivered',30),
                           (%s,'acme','reconciler','not_delivered','INC-N','confirmed absent',30),
                           (%s,'acme','reconciler','not_delivered','INC-R','confirmed absent',15),
                           (%s,'acme','reconciler','not_delivered','INC-T','confirmed absent',11),
                           (%s,'acme','reconciler','not_delivered','INC-M1','first conclusion',30),
                           (%s,'acme','reconciler','delivered','INC-M2','conflicting conclusion',40),
                           (%s,'acme','reconciler','not_delivered','INC-X','missing attempt time',30)""",
                    (identifiers["delivered"], identifiers["not_delivered"],
                     identifiers["reopened"], identifiers["tie"],
                     identifiers["multiple_reconciliations"],
                     identifiers["multiple_reconciliations"], identifiers["missing_time"]),
                )
                conn.execute(
                    """INSERT INTO delivery_attempts(notification_id,number,outcome,
                    http_status,error,started_at,finished_at)
                    VALUES (%s,2,'retryable_failure',503,'ambiguous_network_timeout',20,21)""",
                    (identifiers["reopened"],),
                )
                conn.execute(
                    """INSERT INTO delivery_attempts
                    (notification_id,number,outcome,http_status,error,started_at)
                    VALUES (%s,2,'in_flight',NULL,NULL,20)""",
                    (identifiers["retry_inflight"],),
                )
                conn.execute(
                    """INSERT INTO delivery_reconciliations
                    (notification_id,tenant,actor,outcome,evidence_ref,reason,created_at)
                    VALUES (%s,'acme','reconciler','not_delivered','INC-P','retry scheduled',15),
                           (%s,'acme','reconciler','not_delivered','INC-I','retry scheduled',15)""",
                    (identifiers["retry_pending"], identifiers["retry_inflight"]),
                )
                conn.execute("UPDATE delivery_origins SET in_flight=1")
                conn.execute("UPDATE delivery_capacity SET retained_jobs=%s", (len(names),))

            # Reproduce a database where v6 was committed before the repair release.
            with psycopg.connect(scoped_dsn) as conn:
                conn.execute((migrations / "0006_persist_outcome_uncertainty.sql").read_text())
                # Reproduce the old runbook's terminal status rewrite while retaining evidence.
                conn.execute(
                    "UPDATE delivery_jobs SET status='failed' WHERE id=%s",
                    (identifiers["not_delivered"],),
                )
            with psycopg.connect(scoped_dsn) as conn:
                before_query = conn.execute(
                    """SELECT id,status,outcome_uncertain FROM delivery_jobs ORDER BY id""",
                ).fetchall()
                classifications = {
                    row[0]: row[16]
                    for row in conn.execute(
                        (Path(__file__).resolve().parents[1]
                         / "scripts/inspect_outcome_uncertainty.sql").read_text(),
                    ).fetchall()
                }
                after_query = conn.execute(
                    """SELECT id,status,outcome_uncertain FROM delivery_jobs ORDER BY id""",
                ).fetchall()
                self.assertEqual(before_query, after_query)
                expected_actions = {
                    "success": "clear_uncertainty_keep_succeeded",
                    "overlap_success": "route_terminal_state_to_reconciliation",
                    "inverted_success": "route_terminal_state_to_reconciliation",
                    "equal_start_success": "route_terminal_state_to_reconciliation",
                    "equal_finish_success": "route_terminal_state_to_reconciliation",
                    "delivered": "clear_uncertainty_keep_succeeded",
                    "not_delivered": "clear_uncertainty_keep_failed",
                    "reopened": "retain_uncertainty_new_ambiguity_after_reconciliation",
                    "unresolved": "retain_uncertainty_without_resolution",
                    "tie": "retain_uncertainty_equal_time",
                    "missing_time": "retain_uncertainty_missing_attempt_time",
                    "multiple_reconciliations": "retain_uncertainty_conflicting_reconciliations",
                    "retry_pending": "clear_uncertainty_keep_pending",
                    "retry_inflight": "preserve_inflight_lease_unverified_retry_intent",
                    "paused": "preserve_paused_state_and_uncertainty",
                }
                for name, action in expected_actions.items():
                    self.assertEqual(classifications[identifiers[name]], action, name)
                candidate_rows = conn.execute(
                    (Path(__file__).resolve().parents[1]
                     / "scripts/inspect_outcome_uncertainty.sql").read_text(),
                ).fetchall()
                by_id = {row[0]: row for row in candidate_rows}
                self.assertEqual(by_id[identifiers["not_delivered"]][19], "legacy_unverified")
                self.assertEqual(
                    by_id[identifiers["not_delivered"]][20],
                    "legacy_value_unverified_v6_backfill_or_unknown",
                )
                self.assertTrue(by_id[identifiers["retry_inflight"]][23])

            PostgresStore.migrate(scoped_dsn)
            with psycopg.connect(scoped_dsn) as conn:
                conn.execute(
                    """UPDATE delivery_jobs SET outcome_uncertain=true WHERE id=%s""",
                    (identifiers["retry_pending"],),
                )
                conn.execute(
                    """UPDATE delivery_reconciliations
                    SET created_at=11,retry_requested=true,retry_scheduled=true,
                        retry_decision_source='runtime_recorded'
                    WHERE notification_id=%s""",
                    (identifiers["retry_pending"],),
                )
                runtime_candidate = conn.execute(
                    (Path(__file__).resolve().parents[1]
                     / "scripts/inspect_outcome_uncertainty.sql").read_text(),
                ).fetchall()
                runtime_row = next(
                    row for row in runtime_candidate if row[0] == identifiers["retry_pending"]
                )
                self.assertEqual(runtime_row[16], "preserve_pending_retry_authorization")
                self.assertEqual(runtime_row[19], "runtime_recorded")
                self.assertEqual(runtime_row[20], "runtime_recorded")
        finally:
            with psycopg.connect(DSN) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    def test_v7_upgrade_preserves_retry_state_and_only_clears_proven_terminal_uncertainty(self):
        from dataclasses import replace

        from psycopg.rows import dict_row

        schema = "upgrade_v7_" + uuid.uuid4().hex
        names = (
            "proven_success", "overlap_success", "inverted_success", "equal_start_success",
            "equal_finish_success", "proven_delivered", "retry_pending", "retry_inflight",
            "reopened", "paused", "legacy_failed_uncertain", "closed_not_delivered",
            "unresolved",
        )
        identifiers = {name: uuid.uuid4() for name in names}
        with psycopg.connect(DSN) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped_dsn = DSN + ("&" if "?" in DSN else "?") + urlencode(
            {"options": "-c search_path=" + schema}, quote_via=quote,
        )
        migrations = Path(__file__).resolve().parents[1] / "notifier/production/migrations"
        snapshot = self.security.encode({"request": "retained"})
        try:
            with psycopg.connect(scoped_dsn) as conn:
                conn.execute((Path(__file__).resolve().parents[1]
                              / "notifier/production/schema.sql").read_text())
                for version in range(2, 6):
                    conn.execute(next(migrations.glob(f"{version:04}_*.sql")).read_text())
                conn.execute(
                    "INSERT INTO delivery_origins(origin,in_flight) VALUES (%s,1)",
                    ("https://supplier.example:443",),
                )
                now = conn.execute(
                    "SELECT extract(epoch FROM clock_timestamp())::float8",
                ).fetchone()[0]
                inflight_lease_until = now + 120
                for name, identifier in identifiers.items():
                    status = {
                        "proven_success": "succeeded",
                        "overlap_success": "succeeded",
                        "inverted_success": "succeeded",
                        "equal_start_success": "succeeded",
                        "equal_finish_success": "succeeded",
                        "proven_delivered": "succeeded",
                        "retry_pending": "pending",
                        "retry_inflight": "in_flight",
                        "reopened": "reconciliation_required",
                        "paused": "paused",
                    }.get(name, "failed")
                    attempts = 2 if name in {
                        "proven_success", "overlap_success", "inverted_success",
                        "equal_start_success", "equal_finish_success",
                        "retry_inflight", "reopened",
                    } else 1
                    lease_token = "live-lease" if name == "retry_inflight" else None
                    lease_until = inflight_lease_until if name == "retry_inflight" else None
                    updated_at = now - 31 * 86400 if name == "proven_success" else now
                    conn.execute(
                        """INSERT INTO delivery_jobs
                        (id,tenant,idempotency_key,request_hash,snapshot,origin,retry_mode,status,
                         attempt_count,cycle_attempts,max_attempts,replay_count,next_attempt_at,
                         deadline,lease_token,lease_until,created_at,updated_at)
                        VALUES (%s,'acme',%s,%s,%s,'https://supplier.example:443',
                                'duplicate_tolerant',%s,%s,%s,4,0,%s,%s,%s,%s,%s,%s)""",
                        (
                            identifier, name, name, snapshot, status, attempts, attempts,
                            now if status != "pending" else now - 1, now + 86400,
                            lease_token, lease_until, now - 40 * 86400, updated_at,
                        ),
                    )
                    conn.execute(
                        """INSERT INTO delivery_attempts
                        (notification_id,number,outcome,http_status,error,started_at,finished_at)
                        VALUES (%s,1,'retryable_failure',503,'http_503',10,11)""",
                        (identifier,),
                    )
                for name, outcome, created_at in (
                    ("proven_delivered", "delivered", 12),
                    ("retry_pending", "not_delivered", 12),
                    ("retry_inflight", "not_delivered", 12),
                    ("reopened", "not_delivered", 12),
                    ("closed_not_delivered", "not_delivered", 12),
                ):
                    conn.execute(
                        """INSERT INTO delivery_reconciliations
                        (notification_id,tenant,actor,outcome,evidence_ref,reason,created_at)
                        VALUES (%s,'acme','reconciler',%s,%s,'verified in test',%s)""",
                        (identifiers[name], outcome, "INC-" + name, created_at),
                    )
                conn.execute(
                    """INSERT INTO delivery_attempts
                    (notification_id,number,outcome,http_status,started_at,finished_at)
                    VALUES (%s,2,'succeeded',200,20,21)""",
                    (identifiers["proven_success"],),
                )
                conn.execute(
                    """INSERT INTO delivery_attempts
                    (notification_id,number,outcome,http_status,started_at,finished_at)
                    VALUES (%s,2,'succeeded',200,10.5,12),
                           (%s,2,'succeeded',200,20,19),
                           (%s,2,'succeeded',200,11,12),
                           (%s,2,'succeeded',200,20,20)""",
                    (identifiers["overlap_success"], identifiers["inverted_success"],
                     identifiers["equal_start_success"], identifiers["equal_finish_success"]),
                )
                conn.execute(
                    """INSERT INTO delivery_attempts
                    (notification_id,number,outcome,http_status,error,started_at,finished_at)
                    VALUES (%s,2,'retryable_failure',503,'ambiguous_network_timeout',20,21)""",
                    (identifiers["reopened"],),
                )
                conn.execute(
                    """INSERT INTO delivery_attempts
                    (notification_id,number,outcome,started_at)
                    VALUES (%s,2,'in_flight',20)""",
                    (identifiers["retry_inflight"],),
                )
                conn.execute(
                    """UPDATE delivery_capacity
                    SET retained_jobs=%s,snapshot_bytes=%s WHERE singleton""",
                    (len(names), len(snapshot.encode()) * len(names)),
                )
                conn.execute((migrations / "0006_persist_outcome_uncertainty.sql").read_text())
                # Reproduce a legacy recovery transition after v6 without altering its evidence.
                conn.execute(
                    "UPDATE delivery_jobs SET status='failed' WHERE id=%s",
                    (identifiers["legacy_failed_uncertain"],),
                )
                conn.execute(
                    "UPDATE delivery_jobs SET status='failed' WHERE id=%s",
                    (identifiers["closed_not_delivered"],),
                )

            PostgresStore.migrate(scoped_dsn)
            with psycopg.connect(scoped_dsn) as conn:
                self.assertEqual(conn.execute(
                    "SELECT version FROM delivery_schema",
                ).fetchone()[0], 7)
                columns = {
                    row[0] for row in conn.execute(
                        """SELECT column_name FROM information_schema.columns
                        WHERE table_schema=%s AND table_name='delivery_reconciliations'""",
                        (schema,),
                    )
                }
                self.assertIn("retry_decision_source", columns)
                with conn.cursor(row_factory=dict_row) as cursor:
                    rows = {
                        row["id"]: row for row in cursor.execute(
                        """SELECT id,status,outcome_uncertain,attempt_count,cycle_attempts,
                            replay_count,next_attempt_at,lease_token,lease_until,snapshot
                        FROM delivery_jobs""",
                        ).fetchall()
                    }
                self.assertEqual(
                    (rows[identifiers["proven_success"]]["status"],
                     rows[identifiers["proven_success"]]["outcome_uncertain"]),
                    ("succeeded", False),
                )
                for name in (
                    "overlap_success", "inverted_success", "equal_start_success",
                    "equal_finish_success",
                ):
                    self.assertEqual(
                        (rows[identifiers[name]]["status"],
                         rows[identifiers[name]]["outcome_uncertain"]),
                        ("reconciliation_required", True),
                    )
                self.assertEqual(
                    (rows[identifiers["proven_delivered"]]["status"],
                     rows[identifiers["proven_delivered"]]["outcome_uncertain"]),
                    ("succeeded", False),
                )
                self.assertEqual(
                    (rows[identifiers["retry_pending"]]["status"],
                     rows[identifiers["retry_pending"]]["outcome_uncertain"]),
                    ("pending", False),
                )
                self.assertEqual(rows[identifiers["retry_pending"]]["attempt_count"], 1)
                self.assertEqual(rows[identifiers["retry_pending"]]["cycle_attempts"], 1)
                self.assertEqual(rows[identifiers["retry_pending"]]["replay_count"], 0)
                self.assertEqual(rows[identifiers["retry_pending"]]["next_attempt_at"], now - 1)
                self.assertEqual(
                    (rows[identifiers["retry_inflight"]]["status"],
                     rows[identifiers["retry_inflight"]]["outcome_uncertain"],
                     rows[identifiers["retry_inflight"]]["attempt_count"],
                     rows[identifiers["retry_inflight"]]["lease_token"]),
                    ("in_flight", True, 2, "live-lease"),
                )
                self.assertEqual(
                    rows[identifiers["retry_inflight"]]["lease_until"], inflight_lease_until,
                )
                self.assertEqual(rows[identifiers["retry_inflight"]]["cycle_attempts"], 2)
                self.assertEqual(rows[identifiers["retry_inflight"]]["replay_count"], 0)
                self.assertEqual(rows[identifiers["retry_inflight"]]["next_attempt_at"], now)
                for name in ("retry_pending", "retry_inflight"):
                    self.assertEqual(
                        conn.execute(
                            """SELECT retry_requested,retry_scheduled
                            FROM delivery_reconciliations WHERE notification_id=%s""",
                            (identifiers[name],),
                        ).fetchone(),
                        (True, True),
                    )
                self.assertEqual(
                    (rows[identifiers["reopened"]]["status"],
                     rows[identifiers["reopened"]]["outcome_uncertain"]),
                    ("reconciliation_required", True),
                )
                for name in ("paused", "legacy_failed_uncertain", "unresolved"):
                    self.assertEqual(
                        (rows[identifiers[name]]["status"],
                         rows[identifiers[name]]["outcome_uncertain"]),
                        ("reconciliation_required", True)
                        if name != "paused" else ("paused", True),
                    )
                self.assertEqual(
                    (rows[identifiers["closed_not_delivered"]]["status"],
                     rows[identifiers["closed_not_delivered"]]["outcome_uncertain"]),
                    ("failed", False),
                )
                self.assertTrue(all(row["snapshot"] for row in rows.values()))
                self.assertTrue(all(
                    row[0] == "legacy_unverified"
                    for row in conn.execute(
                        "SELECT retry_decision_source FROM delivery_reconciliations",
                    ).fetchall()
                ))
                repair_audits = conn.execute(
                    """SELECT notification_id,action,reason FROM delivery_audit
                    WHERE actor='schema-migration-v7'""",
                ).fetchall()
                by_notification = {row[0]: row for row in repair_audits}
                self.assertIn(identifiers["closed_not_delivered"], by_notification)
                self.assertEqual(
                    by_notification[identifiers["closed_not_delivered"]][1],
                    "historical_outcome_uncertainty_cleared",
                )
                self.assertIn("not_delivered_failed_terminal",
                              by_notification[identifiers["closed_not_delivered"]][2])
                self.assertIn(identifiers["legacy_failed_uncertain"], by_notification)
                self.assertEqual(
                    by_notification[identifiers["legacy_failed_uncertain"]][1],
                    "historical_outcome_requires_reconciliation",
                )
                audit_count = conn.execute(
                    "SELECT count(*) FROM delivery_audit",
                ).fetchone()[0]
            PostgresStore.migrate(scoped_dsn)
            with psycopg.connect(scoped_dsn) as conn:
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM delivery_audit").fetchone()[0],
                    audit_count,
                )
                self.assertEqual(
                    conn.execute("SELECT version FROM delivery_schema").fetchone()[0], 7,
                )

            isolated_settings = replace(
                self.settings, dsn=scoped_dsn, development=True, per_origin=4,
            )
            store = PostgresStore(isolated_settings, self.security)
            store.open()
            try:
                self.assertTrue(store.ready())
                with store.pool.connection() as conn:
                    self.assertEqual(conn.execute(
                        "SELECT in_flight FROM delivery_origins WHERE origin=%s",
                        ("https://supplier.example:443",),
                    ).fetchone()["in_flight"], 1)
                    inflight_job = conn.execute(
                        """SELECT id,tenant,origin,attempt_count,cycle_attempts,max_attempts,
                        retry_mode,key_expires_at,deadline,lease_token,lease_until,
                        outcome_uncertain FROM delivery_jobs WHERE id=%s""",
                        (identifiers["retry_inflight"],),
                    ).fetchone()
                    self.assertEqual(conn.execute(
                        """SELECT outcome FROM delivery_attempts
                        WHERE notification_id=%s AND number=2""",
                        (identifiers["retry_inflight"],),
                    ).fetchone()["outcome"], "in_flight")
                self.assertTrue(store.finish(
                    inflight_job, Result(success=True, http_status=200), 0,
                ))
                with store.pool.connection() as conn:
                    self.assertEqual(conn.execute(
                        "SELECT in_flight FROM delivery_origins WHERE origin=%s",
                        ("https://supplier.example:443",),
                    ).fetchone()["in_flight"], 0)
                    completed_inflight = conn.execute(
                        "SELECT status,outcome_uncertain FROM delivery_jobs WHERE id=%s",
                        (identifiers["retry_inflight"],),
                    ).fetchone()
                    self.assertEqual(
                        (completed_inflight["status"], completed_inflight["outcome_uncertain"]),
                        ("succeeded", False),
                    )

                pending = store.claim(30)
                self.assertIsNotNone(pending)
                self.assertEqual(pending["id"], str(identifiers["retry_pending"]))
                self.assertTrue(store.finish(
                    pending, Result(success=True, http_status=200), 0,
                ))
                reconciler = Principal("reconciler", "acme", "reconciler")
                closed = store.reconcile(
                    reconciler, identifiers["unresolved"], "not_delivered",
                    "INC-CLOSED-RECHECK", "confirmed absent and do not retry",
                    retry_after_reconciliation=False,
                )
                self.assertFalse(closed["retry_scheduled"])
                with self.assertRaises(Conflict):
                    store.replay(
                        Principal("ops", "acme", "operator"),
                        identifiers["unresolved"],
                        "must not reopen a terminal reconciliation",
                    )
                self.assertEqual(store.maintain(), 1)
                with store.pool.connection() as conn:
                    cleaned = conn.execute(
                        """SELECT status,outcome_uncertain,snapshot IS NULL AS no_snapshot
                        FROM delivery_jobs WHERE id=%s""",
                        (identifiers["proven_success"],),
                    ).fetchone()
                    self.assertEqual(
                        (cleaned["status"], cleaned["outcome_uncertain"], cleaned["no_snapshot"]),
                        ("succeeded", False, True),
                    )
                    self.assertEqual(conn.execute(
                        "SELECT count(*) FROM delivery_attempts WHERE notification_id=%s",
                        (identifiers["proven_success"],),
                    ).fetchone()["count"], 0)
                    unresolved = conn.execute(
                        """SELECT status,outcome_uncertain,snapshot IS NOT NULL AS has_snapshot
                        FROM delivery_jobs WHERE id=%s""",
                        (identifiers["reopened"],),
                    ).fetchone()
                    self.assertEqual(
                        (unresolved["status"], unresolved["outcome_uncertain"],
                         unresolved["has_snapshot"]),
                        ("reconciliation_required", True, True),
                    )
                    self.assertEqual(conn.execute(
                        """SELECT count(*) FROM delivery_attempts WHERE notification_id=%s""",
                        (identifiers["reopened"],),
                    ).fetchone()["count"], 2)
                    self.assertEqual(conn.execute(
                        """SELECT count(*) FROM delivery_reconciliations WHERE notification_id=%s""",
                        (identifiers["reopened"],),
                    ).fetchone()["count"], 1)
            finally:
                store.close()
        finally:
            with psycopg.connect(DSN) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    def test_v7_migration_failure_rolls_back_schema_data_audit_and_version(self):
        schema = "rollback_v7_" + uuid.uuid4().hex
        identifier = uuid.uuid4()
        with psycopg.connect(DSN) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped_dsn = DSN + ("&" if "?" in DSN else "?") + urlencode(
            {"options": "-c search_path=" + schema}, quote_via=quote,
        )
        migrations = Path(__file__).resolve().parents[1] / "notifier/production/migrations"
        try:
            with psycopg.connect(scoped_dsn) as conn:
                conn.execute((Path(__file__).resolve().parents[1]
                              / "notifier/production/schema.sql").read_text())
                for version in range(2, 6):
                    conn.execute(next(migrations.glob(f"{version:04}_*.sql")).read_text())
                conn.execute(
                    "INSERT INTO delivery_origins(origin) VALUES (%s)",
                    ("https://supplier.example:443",),
                )
                conn.execute(
                    """INSERT INTO delivery_jobs
                    (id,tenant,idempotency_key,request_hash,origin,retry_mode,status,
                     attempt_count,cycle_attempts,max_attempts,next_attempt_at,deadline,
                     created_at,updated_at)
                    VALUES (%s,'acme','rollback','rollback-hash','https://supplier.example:443',
                            'duplicate_tolerant','succeeded',1,1,3,1,9999999999,1,2)""",
                    (identifier,),
                )
                conn.execute(
                    """INSERT INTO delivery_attempts
                    (notification_id,number,outcome,http_status,error,started_at,finished_at)
                    VALUES (%s,1,'retryable_failure',503,'http_503',10,11)""",
                    (identifier,),
                )
                conn.execute((migrations / "0006_persist_outcome_uncertainty.sql").read_text())
                conn.execute(
                    """CREATE FUNCTION reject_v7_audit() RETURNS trigger
                    LANGUAGE plpgsql AS $$ BEGIN
                        IF NEW.action LIKE 'historical_outcome_%' THEN
                            RAISE EXCEPTION 'injected v7 audit failure';
                        END IF;
                        RETURN NEW;
                    END $$"""
                )
                conn.execute(
                    """CREATE TRIGGER reject_v7_audit BEFORE INSERT ON delivery_audit
                    FOR EACH ROW EXECUTE FUNCTION reject_v7_audit()"""
                )

            with self.assertRaises(psycopg.Error):
                PostgresStore.migrate(scoped_dsn)
            with psycopg.connect(scoped_dsn) as conn:
                self.assertEqual(
                    conn.execute("SELECT version FROM delivery_schema").fetchone()[0], 6,
                )
                self.assertEqual(conn.execute(
                    "SELECT status,outcome_uncertain FROM delivery_jobs WHERE id=%s",
                    (identifier,),
                ).fetchone(), ("succeeded", True))
                self.assertNotIn("retry_decision_source", {
                    row[0] for row in conn.execute(
                        """SELECT column_name FROM information_schema.columns
                        WHERE table_schema=%s AND table_name='delivery_reconciliations'""",
                        (schema,),
                    )
                })
                self.assertEqual(conn.execute(
                    """SELECT count(*) FROM delivery_audit WHERE action LIKE '%_v7'""",
                ).fetchone()[0], 0)
                conn.execute("DROP TRIGGER reject_v7_audit ON delivery_audit")
                conn.execute("DROP FUNCTION reject_v7_audit()")

            PostgresStore.migrate(scoped_dsn)
            with psycopg.connect(scoped_dsn) as conn:
                self.assertEqual(
                    conn.execute("SELECT version FROM delivery_schema").fetchone()[0], 7,
                )
                self.assertEqual(conn.execute(
                    "SELECT status,outcome_uncertain FROM delivery_jobs WHERE id=%s",
                    (identifier,),
                ).fetchone(), ("reconciliation_required", True))
        finally:
            with psycopg.connect(DSN) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    def test_runbook_hold_recovery_preserves_uncertain_state(self):
        import re

        origin = "https://supplier.example:443"
        identifiers = {
            name: uuid.uuid4() for name in (
                "uncertain", "plain_expired", "safe", "manual_attempted",
                "manual_never_attempted", "idempotency_expired",
            )
        }
        with self.store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO delivery_origins(origin,cooldown_hold,cooldown_reason) "
                "VALUES (%s,true,'test_hold') ON CONFLICT(origin) DO UPDATE "
                "SET cooldown_hold=true,cooldown_reason='test_hold'",
                (origin,),
            )
            now = self.store.now(conn)
            for name, identifier, uncertain, deadline in (
                ("uncertain", identifiers["uncertain"], True, now - 1),
                ("plain_expired", identifiers["plain_expired"], False, now - 1),
                ("safe", identifiers["safe"], False, now + 3600),
                ("manual_attempted", identifiers["manual_attempted"], False, now + 3600),
                ("manual_never_attempted", identifiers["manual_never_attempted"], False, now + 3600),
                ("idempotency_expired", identifiers["idempotency_expired"], False, now + 3600),
            ):
                retry_mode = "idempotent" if name == "idempotency_expired" else (
                    "manual_on_ambiguous" if name.startswith("manual_")
                    else "duplicate_tolerant"
                )
                attempt_count = int(
                    name in {"uncertain", "manual_attempted", "idempotency_expired"},
                )
                conn.execute(
                    """INSERT INTO delivery_jobs
                    (id,tenant,idempotency_key,request_hash,origin,retry_mode,status,
                     outcome_uncertain,attempt_count,cycle_attempts,max_attempts,next_attempt_at,
                     deadline,key_expires_at,created_at,updated_at)
                    VALUES (%s,'acme',%s,%s,%s,%s,'paused',
                            %s,%s,%s,3,%s,%s,%s,%s,%s)""",
                    (
                        identifier, name, name, origin, retry_mode, uncertain,
                        attempt_count, attempt_count, now, deadline,
                        now - 1 if name == "idempotency_expired" else None, now, now,
                    ),
                )
            for name in ("uncertain", "manual_attempted", "idempotency_expired"):
                conn.execute(
                    """INSERT INTO delivery_attempts
                    (notification_id,number,outcome,http_status,error,started_at,finished_at)
                    VALUES (%s,1,'retryable_failure',503,'http_503',%s,%s)""",
                    (identifiers[name], now - 2, now - 1),
                )
            conn.execute("UPDATE delivery_capacity SET retained_jobs=6")
        blocks = re.findall(
            r"```sql\n(.*?)```",
            (Path(__file__).resolve().parents[1] / "docs/production-runbook.md").read_text(),
            flags=re.S,
        )
        restore_sql = next(block for block in blocks if ":send_budget" in block)
        for name in ("origin", "send_budget", "actor", "ticket"):
            restore_sql = restore_sql.replace(":" + name, "%(" + name + ")s")
        with psycopg.connect(self.settings.dsn, autocommit=True,
                             cursor_factory=psycopg.ClientCursor) as conn:
            conn.execute(restore_sql, {
                "origin": origin, "send_budget": self.settings.timeout_seconds + 3,
                "actor": "review-oncall", "ticket": "INC-RECOVERY-TEST",
            })
        with self.store.pool.connection() as conn:
            states = {
                row["id"]: (row["status"], row["outcome_uncertain"])
                for row in conn.execute(
                    "SELECT id,status,outcome_uncertain FROM delivery_jobs WHERE id=ANY(%s)",
                    (list(identifiers.values()),),
                ).fetchall()
            }
            self.assertEqual(states[identifiers["uncertain"]],
                             ("reconciliation_required", True))
            self.assertEqual(states[identifiers["plain_expired"]], ("failed", False))
            self.assertEqual(states[identifiers["safe"]], ("pending", False))
            self.assertEqual(states[identifiers["manual_attempted"]],
                             ("reconciliation_required", True))
            self.assertEqual(states[identifiers["manual_never_attempted"]], ("failed", False))
            self.assertEqual(states[identifiers["idempotency_expired"]], ("failed", False))
            manual_attempt = conn.execute(
                """SELECT outcome,http_status FROM delivery_attempts
                WHERE notification_id=%s AND number=1""",
                (identifiers["manual_attempted"],),
            ).fetchone()
            self.assertEqual(
                (manual_attempt["outcome"], manual_attempt["http_status"]),
                ("retryable_failure", 503),
            )
            self.assertFalse(conn.execute(
                "SELECT cooldown_hold FROM delivery_origins WHERE origin=%s", (origin,),
            ).fetchone()["cooldown_hold"])
            self.assertEqual(conn.execute(
                """SELECT count(*) FROM delivery_audit
                WHERE notification_id=ANY(%s) AND action='retry_after_hold_recovery_blocked'""",
                (list(identifiers.values()),),
            ).fetchone()["count"], 4)
            self.assertEqual(conn.execute(
                """SELECT count(*) FROM delivery_audit
                WHERE notification_id=%s AND action='manual_paused_task_quarantined'""",
                (identifiers["manual_attempted"],),
            ).fetchone()["count"], 1)

    def test_cooldown_release_keeps_manual_result_unresolved_and_allows_safe_contract_retry(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from notifier.delivery import RetryPolicy
        from notifier.worker import Worker

        effects = []
        calls = {}
        accepted_keys = set()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                calls[self.path] = calls.get(self.path, 0) + 1
                key = self.headers.get("Idempotency-Key")
                if not key or key not in accepted_keys:
                    effects.append(self.path)
                    if key:
                        accepted_keys.add(key)
                self.send_response(503 if calls[self.path] == 1 else 200)
                if calls[self.path] == 1:
                    self.send_header("Retry-After", "9" * 80)
                self.send_header("Content-Length", "0")
                self.end_headers()

        supplier = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=supplier.serve_forever, daemon=True)
        server_thread.start()
        origin = f"http://127.0.0.1:{supplier.server_port}"
        self.settings.origins = (origin,)
        self.credentials.write_text(json.dumps({
            "unsafe": {
                "supplier_account": "local-no-idempotency-contract",
                "tenants": ["acme"], "origins": [origin],
                "retry_mode": "manual_on_ambiguous",
                "operations": [{"method": "POST", "path": "/vendor/orders/unsafe", "query": {}}],
                "headers": {},
            },
            "safe": {
                "supplier_account": "local-idempotency-contract",
                "tenants": ["acme"], "origins": [origin],
                "retry_mode": "idempotent", "deduplication_window_seconds": 3600,
                "operations": [{"method": "POST", "path": "/vendor/orders/safe", "query": {}}],
                "headers": {},
            },
        }))
        self.security = security_for(self.settings)
        self.store.security = self.security
        identifier = self.submit_job(request={
            "url": origin + "/vendor/orders/unsafe", "credential_ref": "unsafe",
            "headers": {"Content-Type": "application/json"}, "body": '{"order_id":"once"}',
        })
        safe_identifier = self.submit_job(request={
            "url": origin + "/vendor/orders/safe", "credential_ref": "safe",
            "headers": {"Content-Type": "application/json", "Idempotency-Key": "stable-key"},
            "body": '{"order_id":"safe-once"}',
        })
        worker = Worker(
            self.store, SupplierTransport(self.security, 1), RetryPolicy(.01, .01, 0),
        )
        try:
            self.assertTrue(worker.run_once())
            item = self.store.get("acme", identifier)
            self.assertEqual(item["status"], "reconciliation_required")
            self.assertEqual(item["last_error"], "manual_reconciliation_required")
            self.assertEqual(effects, ["/vendor/orders/unsafe"])
            self.assertEqual(self.store.get("acme", safe_identifier)["status"], "pending")
            with self.store.pool.connection() as conn:
                origin_row = conn.execute(
                    "SELECT cooldown_hold,cooldown_reason FROM delivery_origins",
                ).fetchone()
                self.assertTrue(origin_row["cooldown_hold"])
                self.assertEqual(
                    origin_row["cooldown_reason"], "retry_after_exceeds_supported_range",
                )
                now = self.store.now(conn)
                conn.execute(
                    """UPDATE delivery_origins SET cooldown_hold=false,cooldown_reason=NULL,
                    next_allowed=%s WHERE origin=%s""", (now, origin),
                )
            self.assertTrue(worker.run_once())
            self.assertEqual(self.store.get("acme", safe_identifier)["status"], "paused")
            with self.store.pool.connection() as conn:
                conn.execute(
                    """UPDATE delivery_origins SET cooldown_hold=false,cooldown_reason=NULL,
                    next_allowed=%s WHERE origin=%s""", (self.store.now(conn), origin),
                )
                conn.execute(
                    """UPDATE delivery_jobs SET status='pending',next_attempt_at=%s,
                    last_error='retry_after_hold_released',updated_at=%s
                    WHERE origin=%s AND status='paused'
                    AND retry_mode IN ('idempotent','duplicate_tolerant')
                    AND deadline>%s+%s AND cycle_attempts<max_attempts
                    AND (retry_mode<>'idempotent' OR
                         (key_expires_at IS NOT NULL AND key_expires_at>%s+%s))""",
                    (self.store.now(conn), self.store.now(conn), origin,
                     self.store.now(conn), self.settings.timeout_seconds + 3,
                     self.store.now(conn), self.settings.timeout_seconds + 3),
                )
            self.assertTrue(worker.run_once())
            self.assertEqual(self.store.get("acme", identifier)["status"], "reconciliation_required")
            self.assertEqual(self.store.get("acme", safe_identifier)["status"], "succeeded")
            self.assertEqual(calls["/vendor/orders/unsafe"], 1)
            self.assertEqual(calls["/vendor/orders/safe"], 2)
            self.assertEqual(effects.count("/vendor/orders/unsafe"), 1)
            self.assertEqual(effects.count("/vendor/orders/safe"), 1)
            hold_audit = self.store.audit("acme", limit=20)["items"]
            self.assertTrue(any(
                item["action"] == "retry_after_hold"
                and item["reason"] == "retry_after_exceeds_supported_range"
                for item in hold_audit
            ))
        finally:
            supplier.shutdown()
            supplier.server_close()
            server_thread.join()

    def test_full_duration_connect_timeout_transitions_to_retryable_store_state(self):
        import time
        from unittest.mock import Mock

        from notifier.delivery import RetryPolicy
        from notifier.worker import Worker

        self.security.policy.resolver = lambda _host, timeout=None: ["93.184.216.34"]

        def consume_timeout(_address, timeout):
            time.sleep(timeout + .01)
            raise TimeoutError("connection timed out")

        transport = SupplierTransport(self.security, .03)
        worker = Worker(self.store, transport, RetryPolicy(.5, .5, 0), lease_seconds=30)
        identifier = self.submit_job()
        with patch("notifier.delivery.socket.create_connection", side_effect=consume_timeout):
            self.assertTrue(worker.run_once())
        item = self.store.get("acme", identifier)
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["last_error"], "network_timeout")
        self.assertEqual(item["attempts"][-1]["outcome"], "retryable_failure")
        self.assertIsNone(self.store.claim(30))  # Backoff prevents an immediate second send.
        self.assertEqual(self.store.stats()["open_circuits"], 0)

        class TimedSocket:
            timeout = None

            def settimeout(self, timeout):
                self.timeout = timeout

            def shutdown(self, _how):
                pass

            def close(self):
                pass

        def consume_tls_timeout(sock, server_hostname):
            time.sleep(sock.timeout + .01)
            raise TimeoutError("TLS handshake timed out")

        socket_obj = TimedSocket()
        tls_context = Mock()
        tls_context.wrap_socket.side_effect = consume_tls_timeout
        tls_identifier = self.submit_job()
        with self.store.pool.connection() as conn:
            conn.execute("UPDATE delivery_origins SET next_allowed=0")
        with patch("notifier.delivery.socket.create_connection", return_value=socket_obj):
            with patch("notifier.delivery.ssl.create_default_context", return_value=tls_context):
                self.assertTrue(worker.run_once())
        tls_item = self.store.get("acme", tls_identifier)
        self.assertEqual(tls_item["status"], "pending")
        self.assertEqual(tls_item["last_error"], "network_timeout")
        self.assertEqual(tls_item["attempts"][-1]["outcome"], "retryable_failure")

    def test_hold_does_not_block_expired_lease_recovery_under_competing_workers(self):
        import concurrent.futures

        registry = json.loads(self.credentials.read_text())
        registry["crm"]["retry_mode"] = "manual_on_ambiguous"
        self.credentials.write_text(json.dumps(registry))
        first, second = self.submit_job(), self.submit_job()
        with self.store.pool.connection() as conn:
            conn.execute("UPDATE delivery_origins SET next_allowed=0")
        expired = self.store.claim(30)
        hold_trigger = self.store.claim(30)
        self.assertEqual({expired["id"], hold_trigger["id"]}, {first, second})
        self.assertTrue(self.store.finish(hold_trigger, Result(
            retryable=True, error="retry_after_exceeds_supported_range",
            cooldown_hold=True, failure_class="supplier_service",
        ), 0))
        self.force_expire(expired["id"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            claims = list(executor.map(lambda _index: self.store.claim(30), range(4)))
        self.assertTrue(all(job is None for job in claims))
        recovered = self.store.get("acme", expired["id"])
        self.assertEqual(recovered["status"], "reconciliation_required")
        self.assertEqual(recovered["attempts"][-1]["outcome"], "abandoned")
        with self.store.pool.connection() as conn:
            origin = conn.execute(
                "SELECT in_flight,cooldown_hold FROM delivery_origins",
            ).fetchone()
        self.assertEqual(origin["in_flight"], 0)
        self.assertTrue(origin["cooldown_hold"])

    def test_final_ambiguous_lease_expiry_requires_reconciliation(self):
        registry = json.loads(self.credentials.read_text())
        registry["crm"].update(
            retry_mode="idempotent", deduplication_window_seconds=3600,
        )
        self.credentials.write_text(json.dumps(registry))
        self.settings.max_attempts = 1
        identifier = self.submit_job(request={
            **self.request, "headers": {"Idempotency-Key": "last-attempt-key"},
        })
        claimed = self.store.claim(30)
        self.assertEqual(claimed["id"], identifier)
        self.force_expire(identifier)
        self.assertIsNone(self.store.claim(30))
        item = self.store.get("acme", identifier)
        self.assertEqual(item["status"], "reconciliation_required")
        self.assertEqual(item["last_error"], "worker_lease_expired")
        self.assertEqual(item["attempts"][-1]["outcome"], "abandoned")
        with self.store.pool.connection() as conn:
            conn.execute("UPDATE delivery_jobs SET updated_at=0 WHERE id=%s", (identifier,))
        self.assertEqual(self.store.maintain(), 0)
        self.assertIsNotNone(self.store.get("acme", identifier))

    def test_prior_uncertainty_survives_later_local_authorization_failure(self):
        registry = json.loads(self.credentials.read_text())
        registry["crm"].update(
            retry_mode="idempotent", deduplication_window_seconds=3600,
        )
        self.credentials.write_text(json.dumps(registry))
        identifier = self.submit_job(request={
            **self.request, "headers": {"Idempotency-Key": "history-key"},
        })
        first = self.store.claim(30)
        self.assertTrue(self.store.finish(first, Result(
            retryable=True, ambiguous=True, request_state="possibly_sent",
            error="ambiguous_network_timeout", failure_class="supplier_network",
        ), 0))
        second = self.store.claim(30)
        self.assertEqual(second["id"], identifier)
        registry["crm"]["operations"] = [
            {"method": "POST", "path": "/different", "query": {}},
        ]
        self.credentials.write_text(json.dumps(registry))
        rejected = SupplierTransport(self.security, 1).send(second["request"])
        self.assertEqual(rejected.error, "credential_authorization_rejected")
        self.assertTrue(self.store.finish(second, rejected, 0))
        item = self.store.get("acme", identifier)
        self.assertEqual(item["status"], "reconciliation_required")
        self.assertEqual(
            [attempt["error"] for attempt in item["attempts"]],
            ["ambiguous_network_timeout", "credential_authorization_rejected"],
        )
        with self.store.pool.connection() as conn:
            conn.execute("UPDATE delivery_jobs SET updated_at=0 WHERE id=%s", (identifier,))
        self.assertEqual(self.store.maintain(), 0)
        self.assertIsNotNone(self.store.get("acme", identifier))

    def test_pre_send_failure_without_uncertain_history_remains_failed(self):
        identifier = self.submit_job()
        job = self.store.claim(30)
        self.assertTrue(self.store.finish(job, Result(
            error="credential_authorization_rejected",
            failure_class="local_authorization",
            request_state="not_sent",
        ), 0))
        self.assertEqual(self.store.get("acme", identifier)["status"], "failed")

    def test_reconciler_can_close_confirmed_absence_without_retry(self):
        identifier = self.submit_job()
        claimed = self.store.claim(30)
        self.assertTrue(self.store.finish(
            claimed, Result(error="manual_reconciliation_required"), 0,
        ))
        result = self.store.reconcile(
            Principal("reconciler", "acme", "reconciler"), identifier,
            "not_delivered", "INC-CLOSE", "verified absent; no further delivery",
            retry_after_reconciliation=False,
        )
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["retry_requested"])
        self.assertFalse(result["retry_scheduled"])
        detail = self.store.get("acme", identifier)
        self.assertEqual(detail["reconciliations"][0]["outcome"], "not_delivered")
        self.assertEqual(detail["replay_count"], 0)
        self.assertFalse(detail["replayable"])
        self.assertIsNone(self.store.claim(30))

    def test_replay_limit_allows_terminal_reconciliation_but_not_another_send(self):
        no_snapshot = self.submit_job()
        expired_key_registry = json.loads(self.credentials.read_text())
        expired_key_registry["crm"].update(
            retry_mode="idempotent", deduplication_window_seconds=3600,
        )
        self.credentials.write_text(json.dumps(expired_key_registry))
        expired_key = self.submit_job(request={
            **self.request, "headers": {"Idempotency-Key": "expired-key"},
        })
        active_key = self.submit_job(request={
            **self.request, "headers": {"Idempotency-Key": "active-key"},
        })
        with self.store.pool.connection() as conn:
            now = self.store.now(conn)
            conn.execute(
                """UPDATE delivery_jobs SET status='reconciliation_required',replay_count=10,
                snapshot=NULL WHERE id=%s""", (no_snapshot,),
            )
            conn.execute(
                """UPDATE delivery_jobs SET status='reconciliation_required',replay_count=10,
                key_expires_at=%s WHERE id=%s""", (now + 1, expired_key),
            )
            conn.execute(
                """UPDATE delivery_jobs SET status='reconciliation_required',replay_count=10,
                key_expires_at=%s WHERE id=%s""", (now + 3600, active_key),
            )
        reconciler = Principal("reconciler", "acme", "reconciler")
        absent_snapshot = self.store.reconcile(
            reconciler, no_snapshot, "not_delivered", "INC-NOSNAPSHOT", "confirmed absent",
        )
        absent_expired_key = self.store.reconcile(
            reconciler, expired_key, "not_delivered", "INC-EXPIRED", "confirmed absent",
        )
        self.assertEqual(absent_snapshot["status"], "failed")
        self.assertEqual(absent_expired_key["status"], "failed")
        self.assertEqual(absent_snapshot["next_action"], "submit_new_notification_with_fresh_supplier_key")
        self.assertEqual(absent_expired_key["next_action"], "submit_new_operation_with_fresh_supplier_key")
        for identifier, expected_outcome in (
            (no_snapshot, "not_delivered"),
            (expired_key, "not_delivered"),
        ):
            item = self.store.get("acme", identifier)
            self.assertEqual(item["replay_count"], 10)
            self.assertEqual(item["reconciliations"][0]["outcome"], expected_outcome)
        audit_actions = [item["action"] for item in self.store.audit("acme", limit=50)["items"]]
        self.assertGreaterEqual(audit_actions.count("reconcile_not_delivered"), 2)
        terminal = self.store.reconcile(
            reconciler, active_key, "not_delivered", "INC-REPLAY-LIMIT", "confirmed absent",
        )
        self.assertEqual(terminal["status"], "failed")
        self.assertFalse(terminal["retry_scheduled"])
        self.assertEqual(terminal["retry_requested"], True)
        active_item = self.store.get("acme", active_key)
        self.assertEqual(active_item["status"], "failed")
        self.assertEqual(active_item["replay_count"], 10)
        self.assertFalse(active_item["replayable"])
        self.assertEqual(active_item["reconciliations"][0]["outcome"], "not_delivered")
        self.assertFalse(active_item["reconciliations"][0]["retry_scheduled"])
        with self.assertRaises(Conflict):
            self.store.replay(reconciler, active_key, "must not bypass cap")
        self.assertIsNone(self.store.claim(30))
        for identifier in (no_snapshot, expired_key, active_key):
            self.assertEqual(self.store.get("acme", identifier)["attempt_count"], 0)

    def test_local_configuration_failure_does_not_pollute_shared_circuit(self):
        identifier = self.submit_job()
        job = self.store.claim(30)
        with self.store.pool.connection() as conn:
            conn.execute(
                "UPDATE delivery_origins SET failures=2,open_until=0 WHERE origin=%s",
                (job["origin"],),
            )
        self.store.finish(job, Result(
            retryable=True, error="credential_configuration_unavailable",
            failure_class="local_configuration",
        ), 0)
        with self.store.pool.connection() as conn:
            origin = conn.execute(
                "SELECT failures,open_until FROM delivery_origins WHERE origin=%s",
                (job["origin"],),
            ).fetchone()
        self.assertEqual(origin["failures"], 2)
        self.assertEqual(origin["open_until"], 0)
        self.assertEqual(self.store.get("acme", identifier)["status"], "pending")

    def test_submit_receipt_lookup_survives_profile_revocation_but_new_submit_does_not(self):
        response = self.client.post(
            "/v1/notifications", headers=self.headers, json=self.request,
        )
        self.assertEqual(response.status_code, 202)
        identifier = response.json()["id"]
        registry = json.loads(self.credentials.read_text())
        registry["crm"]["operations"] = [{"method": "POST", "path": "/revoked", "query": {}}]
        self.credentials.write_text(json.dumps(registry))

        duplicate = self.client.post(
            "/v1/notifications", headers=self.headers, json=self.request,
        )
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertEqual(duplicate.json()["id"], identifier)
        self.assertFalse(duplicate.json()["created"])

        different = self.client.post(
            "/v1/notifications",
            headers={**self.headers, "Idempotency-Key": "another-key"},
            json=self.request,
        )
        self.assertEqual(different.status_code, 400)

        mismatch = self.client.post(
            "/v1/notifications", headers=self.headers,
            json={**self.request, "body": "different"},
        )
        self.assertEqual(mismatch.status_code, 409)

    def test_submit_receipt_lookup_survives_global_origin_revocation(self):
        auth = json.loads(self.auth.read_text())
        auth.append({
            "name": "other-client", "tenant": "other", "role": "sender",
            "token_sha256": hashlib.sha256(b"z" * 40).hexdigest(),
        })
        self.auth.write_text(json.dumps(auth))
        registry = json.loads(self.credentials.read_text())
        registry["crm"]["tenants"].append("other")
        self.credentials.write_text(json.dumps(registry))
        response = self.client.post(
            "/v1/notifications", headers=self.headers, json=self.request,
        )
        self.assertEqual(response.status_code, 202)
        identifier = response.json()["id"]
        other_tenant = self.client.post(
            "/v1/notifications",
            headers={
                "Authorization": "Bearer " + "z" * 40,
                "Idempotency-Key": self.headers["Idempotency-Key"],
            },
            json=self.request,
        )
        self.assertEqual(other_tenant.status_code, 202, other_tenant.text)
        self.assertNotEqual(other_tenant.json()["id"], identifier)
        self.security.policy.origins.clear()

        duplicate = self.client.post(
            "/v1/notifications", headers=self.headers, json=self.request,
        )
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertEqual(duplicate.json()["id"], identifier)

        mismatch = self.client.post(
            "/v1/notifications", headers=self.headers,
            json={**self.request, "body": "different"},
        )
        self.assertEqual(mismatch.status_code, 409)

        fresh = self.client.post(
            "/v1/notifications",
            headers={**self.headers, "Idempotency-Key": "fresh-after-revoke"},
            json=self.request,
        )
        self.assertEqual(fresh.status_code, 400)

        job = self.store.claim(30)
        self.assertEqual(job["id"], identifier)
        result = SupplierTransport(self.security, 1).send(job["request"])
        self.assertEqual(result.failure_class, "local_authorization")
        self.assertFalse(result.retryable)

    def test_lease_recovery_fences_old_worker_and_resets_origin_slot(self):
        identifier = self.submit_job()
        old = self.store.claim(30)
        self.force_expire(identifier)
        new = self.store.claim(30)
        self.assertEqual(new["id"], old["id"])
        self.assertNotEqual(new["lease_token"], old["lease_token"])
        self.assertFalse(self.store.heartbeat(old, 30))
        self.assertFalse(self.store.finish(old, Result(success=True), 0))
        self.assertTrue(self.store.heartbeat(new, 30))
        self.assertTrue(self.store.finish(new, Result(success=True), 0))
        item = self.store.get("acme", identifier)
        self.assertEqual(item["status"], "succeeded")
        self.assertEqual([attempt["outcome"] for attempt in item["attempts"]], ["abandoned", "succeeded"])
        with self.store.pool.connection() as conn:
            self.assertEqual(conn.execute("SELECT in_flight FROM delivery_origins").fetchone()["in_flight"], 0)

    def test_exhausted_crash_and_deadline_do_not_run_forever(self):
        self.settings.max_attempts = 1
        identifier = self.submit_job()
        self.store.claim(30)
        self.force_expire(identifier)
        self.assertIsNone(self.store.claim(30))
        crashed = self.store.get("acme", identifier)
        self.assertEqual(crashed["status"], "reconciliation_required")
        self.assertTrue(crashed["outcome_uncertain"])
        deadline = self.submit_job()
        with self.store.pool.connection() as conn:
            conn.execute("UPDATE delivery_jobs SET deadline=0 WHERE id=%s", (deadline,))
        self.assertIsNone(self.store.claim(30))
        self.assertEqual(self.store.get("acme", deadline)["last_error"], "delivery_deadline_exceeded")

    def test_capacity_duplicates_and_tenant_scoping(self):
        self.settings.max_pending = 1
        identifier = self.submit_job("fixed")
        self.assertEqual(self.submit_job("fixed"), identifier)
        with self.assertRaises(CapacityExceeded):
            self.submit_job("new")
        self.assertIsNone(self.store.get("other", identifier))
        self.assertEqual(self.store.list("other")["items"], [])
        self.assertFalse(self.store.replay(Principal("operator", "other", "operator"), identifier, "recover"))

    def test_replay_audit_and_pagination(self):
        identifiers = [self.submit_job() for _ in range(3)]
        page = self.store.list("acme", limit=2)
        tail = self.store.list("acme", before=page["next_cursor"], limit=2)
        self.assertEqual(len({x["id"] for x in page["items"] + tail["items"]}), 3)
        job = self.store.claim(30)
        self.store.finish(job, Result(http_status=400, error="http_400"), 0)
        operator = Principal("ops", "acme", "operator")
        self.assertTrue(self.store.replay(operator, job["id"], "Supplier fix verified"))
        self.assertEqual(self.store.get("acme", job["id"])["replay_count"], 1)
        audit = self.store.audit("acme", limit=2)
        self.assertEqual(audit["items"][0]["action"], "replay")
        self.assertEqual(len(self.store.audit("acme", before=audit["next_cursor"])["items"]), 2)
        self.assertEqual(len(identifiers), 3)

    def test_retention_keeps_idempotency_tombstone(self):
        identifier = self.submit_job("retained")
        job = self.store.claim(30)
        self.store.finish(job, Result(http_status=400), 0)
        with self.store.pool.connection() as conn:
            conn.execute("UPDATE delivery_jobs SET updated_at=extract(epoch FROM now())-31*86400")
        self.assertEqual(self.store.maintain(), 1)
        self.assertFalse(self.store.get("acme", identifier)["replayable"])
        self.assertEqual(self.submit_job("retained"), identifier)
        with self.assertRaises(PayloadExpired):
            self.store.replay(Principal("ops", "acme", "operator"), identifier, "recover")

    def test_set_based_maintenance_is_consistent_with_concurrent_submit_claim_and_heartbeat(self):
        terminal = []
        for _ in range(3):
            identifier = self.submit_job()
            job = self.store.claim(30)
            self.assertEqual(job["id"], identifier)
            self.store.finish(job, Result(success=True, http_status=204), 0)
            terminal.append(identifier)
        with self.store.pool.connection() as conn:
            conn.execute(
                """UPDATE delivery_jobs SET updated_at=extract(epoch FROM clock_timestamp())-
                CASE WHEN id=%s THEN 91 ELSE 31 END*86400 WHERE id=ANY(%s::uuid[])""",
                (terminal[-1], terminal),
            )
        active = self.submit_job()
        active_job = self.store.claim(30)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            maintenance = executor.submit(self.store.maintain, 10)
            submission = executor.submit(self.submit_job, "during-maintenance")
            claim = executor.submit(self.store.claim, 30)
            heartbeat = executor.submit(self.store.heartbeat, active_job, 30)
            maintenance.result()
            submitted = submission.result()
            claimed = claim.result()
            self.assertTrue(heartbeat.result())
        self.assertIsNotNone(submitted)
        deadline = time.monotonic() + 2
        while claimed is None and time.monotonic() < deadline:
            time.sleep(.01)
            claimed = self.store.claim(30)
        self.assertIsNotNone(claimed)
        with self.store.pool.connection() as conn:
            counters = conn.execute(
                "SELECT snapshot_bytes,retained_jobs FROM delivery_capacity",
            ).fetchone()
            actual = conn.execute(
                """SELECT coalesce(sum(octet_length(snapshot)),0) AS bytes,count(*) AS jobs
                FROM delivery_jobs""",
            ).fetchone()
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM delivery_attempts WHERE notification_id=%s",
                    (terminal[-1],),
                ).fetchone()["count"],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM delivery_jobs WHERE id=%s", (terminal[-1],),
                ).fetchone()["count"],
                0,
            )
        self.assertEqual(counters["snapshot_bytes"], actual["bytes"])
        self.assertEqual(counters["retained_jobs"], actual["jobs"])
        self.assertEqual(self.store.get("acme", active)["status"], "in_flight")

    def test_rotate_all_snapshots_with_live_keyring(self):
        identifier = self.submit_job()
        config = json.loads(self.keys.read_text())
        config["encryption_keys"].insert(0, Fernet.generate_key().decode())
        self.keys.write_text(json.dumps(config))
        self.assertEqual(self.store.rotate_snapshots(), 1)
        config["encryption_keys"] = config["encryption_keys"][:1]
        self.keys.write_text(json.dumps(config))
        self.assertEqual(self.store.claim(30)["id"], identifier)

    def test_worker_freshness_and_metrics(self):
        self.assertEqual(self.store.stats()["healthy_workers"], 0)
        self.store.worker_seen("instance-one")
        self.assertEqual(self.store.stats()["healthy_workers"], 1)
        auth = json.loads(self.auth.read_text())
        auth.append({"name": "prometheus", "tenant": "system", "role": "metrics",
                     "token_sha256": hashlib.sha256(b"m" * 40).hexdigest()})
        self.auth.write_text(json.dumps(auth))
        self.assertEqual(self.client.get("/metrics", headers=self.headers).status_code, 403)
        response = self.client.get("/metrics", headers={"Authorization": "Bearer " + "m" * 40})
        self.assertEqual(response.status_code, 200)
        self.assertIn("notify_healthy_workers 1.0", response.text)
        self.assertNotIn("supplier-secret", response.text)
        self.assertEqual(self.client.get("/v1/stats", headers={
            "Authorization": "Bearer " + "m" * 40,
        }).status_code, 403)

    def test_http_authorization_validation_and_quota(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/readyz").status_code, 200)
        self.assertEqual(self.client.get("/v1/stats").status_code, 401)
        response = self.client.post("/v1/notifications", json=self.request, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        identifier = response.json()["id"]
        duplicate = self.client.post("/v1/notifications", json=self.request, headers=self.headers)
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(self.client.get("/v1/notifications/" + identifier, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post("/v1/notifications/" + identifier + "/replay",
                                         json={"reason": "repair"}, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.get("/v1/audit", headers=self.headers).status_code, 403)
        self.assertEqual(self.client.get("/v1/notifications?cursor=bad", headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post("/v1/notifications", content='{"body":1,"body":2}',
                                         headers={**self.headers, "Content-Type": "application/json"}).status_code, 400)
        self.assertEqual(self.client.post("/v1/notifications", content="x" * 262145,
                                         headers={**self.headers, "Content-Type": "application/json"}).status_code, 413)
        self.settings.max_pending = 1
        limited = self.client.post("/v1/notifications", json=self.request,
                                   headers={**self.headers, "Idempotency-Key": "new"})
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["Retry-After"], "10")

    def test_api_storage_outage_never_acknowledges(self):
        with patch.object(self.store, "submit", side_effect=psycopg.OperationalError("secret-dsn")):
            response = self.client.post("/v1/notifications", json=self.request, headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret-dsn", response.text)
        self.assertEqual(self.store.stats()["notifications"]["pending"], 0)

    def test_real_process_crash_recovered_by_other_pool(self):
        identifier = self.submit_job()
        env = {
            **os.environ, "NOTIFY_ENV": "development", "NOTIFY_DATABASE_URL": self.settings.dsn,
            "NOTIFY_AUTH_FILE": str(self.auth), "NOTIFY_KEYS_FILE": str(self.keys),
            "NOTIFY_CREDENTIALS_FILE": str(self.credentials),
            "NOTIFY_ALLOWED_ORIGINS": ",".join(self.settings.origins),
        }
        env.pop("NOTIFY_DATABASE_URL_FILE", None)
        source = (
            "from notifier.production.config import Settings; "
            "from notifier.production.runtime import security_for; "
            "from notifier.production.store import PostgresStore; "
            "import os; s=Settings.from_env(); db=PostgresStore(s,security_for(s)); db.open(); "
            "print(db.claim(5)['id'],flush=True); os._exit(17)"
        )
        result = subprocess.run([sys.executable, "-c", source], check=False, env=env, capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(result.stdout.strip(), identifier)
        time.sleep(5.1)
        replacement = self.store.claim(30)
        self.assertEqual(replacement["id"], identifier)
        self.assertTrue(self.store.finish(replacement, Result(success=True), 0))

    def test_separate_api_worker_processes_deliver_to_real_mock_http(self):
        import socket

        import httpx

        from notifier.mock_supplier import supplier_server

        supplier = supplier_server(port=0)
        supplier.start()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.credentials.write_text(json.dumps({
            "orders": {"supplier_account": "mock-orders", "tenants": ["acme"],
                       "origins": [supplier.url],
                       "retry_mode": "idempotent",
                       "deduplication_window_seconds": 86400,
                       "operations": [{"method": "POST", "path": "/vendor/orders", "query": {}}],
                       "headers": {"X-API-Key": "supplier-demo-secret"}},
        }))
        env = {
            **os.environ, "NOTIFY_ENV": "development",
            "NOTIFY_DATABASE_URL": self.settings.dsn, "NOTIFY_AUTH_FILE": str(self.auth),
            "NOTIFY_KEYS_FILE": str(self.keys), "NOTIFY_CREDENTIALS_FILE": str(self.credentials),
            "NOTIFY_ALLOWED_ORIGINS": supplier.url, "NOTIFY_WORKERS": "2", "NOTIFY_PER_ORIGIN": "1",
        }
        env.pop("NOTIFY_DATABASE_URL_FILE", None)
        api_log = self.directory / "api.log"
        worker_log = self.directory / "worker.log"
        children = []
        try:
            with api_log.open("w") as api_output, worker_log.open("w") as worker_output:
                children.append(subprocess.Popen(
                    [sys.executable, "-m", "notifier.production", "api", "--port", str(port)],
                    env=env, stdout=api_output, stderr=api_output,
                ))
                children.append(subprocess.Popen(
                    [sys.executable, "-m", "notifier.production", "worker"],
                    env=env, stdout=worker_output, stderr=worker_output,
                ))
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False) as client:
                    deadline = time.monotonic() + 12
                    while True:
                        try:
                            if client.get("/readyz").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() > deadline:
                            self.fail(api_log.read_text())
                        time.sleep(.1)
                    response = client.post("/v1/notifications", headers=self.headers, json={
                        "url": supplier.url + "/vendor/orders", "credential_ref": "orders",
                        "headers": {"Content-Type": "application/json", "Idempotency-Key": "order-e2e"},
                        "body": '{"order_id":"process-e2e","amount_cents":123}',
                    })
                    self.assertEqual(response.status_code, 202, response.text)
                    identifier = response.json()["id"]
                    while True:
                        result = client.get("/v1/notifications/" + identifier, headers=self.headers)
                        if result.json()["status"] == "succeeded":
                            break
                        if time.monotonic() > deadline:
                            self.fail(worker_log.read_text() + result.text)
                        time.sleep(.1)
                for child in children:
                    child.terminate()
                # Uvicorn re-raises a captured SIGTERM after graceful ASGI shutdown.
                self.assertIn(children[0].wait(timeout=10), (0, -signal.SIGTERM))
                self.assertEqual(children[1].wait(timeout=10), 0)
            self.assertEqual(supplier.state.snapshot()["effects"]["process-e2e"], 1)
            self.assertIn("Application shutdown complete", api_log.read_text())
            self.assertNotIn("supplier-demo-secret", api_log.read_text() + worker_log.read_text())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
            supplier.stop()

    def test_http_tenant_operator_replay_and_audit_boundaries(self):
        auth = json.loads(self.auth.read_text())
        auth.extend([
            {"name": "outsider", "tenant": "other", "role": "operator",
             "token_sha256": hashlib.sha256(b"x" * 40).hexdigest()},
        ])
        self.auth.write_text(json.dumps(auth))
        identifier = self.submit_job()
        job = self.store.claim(30)
        self.store.finish(job, Result(http_status=400), 0)
        path = "/v1/notifications/" + identifier
        outside = {"Authorization": "Bearer " + "x" * 40}
        operator = {"Authorization": "Bearer " + "o" * 40}
        self.assertEqual(self.client.get(path, headers=outside).status_code, 404)
        self.assertEqual(self.client.post(path + "/replay", headers=outside,
                                         json={"reason": "recover"}).status_code, 404)
        self.assertEqual(self.client.post(path + "/replay", headers=operator,
                                         json={}).status_code, 400)
        self.assertEqual(self.client.post(path + "/replay", headers=operator,
                                         json={"reason": "fix confirmed"}).status_code, 202)
        audit = self.client.get("/v1/audit", headers=operator).json()
        self.assertEqual(audit["items"][0]["actor"], "ops")
        self.assertEqual(self.client.get("/v1/audit", headers=outside).json()["items"], [])

    def test_retry_exhaustion_and_tenant_specific_quota(self):
        self.settings.max_attempts = 2
        self.settings.max_tenant_pending = 1
        identifier = self.submit_job()
        with self.assertRaises(CapacityExceeded):
            self.submit_job()
        other = Principal("other", "other", "sender")
        other_id = self.submit_job(principal=other, request={**self.request, "credential_ref": "other"})
        job = self.store.claim(30)
        self.assertEqual(job["id"], identifier)
        self.store.finish(job, Result(retryable=True, http_status=503), 0)
        with self.store.pool.connection() as conn:
            conn.execute("UPDATE delivery_origins SET next_allowed=0")
            conn.execute("UPDATE delivery_jobs SET next_attempt_at=deadline WHERE id=%s", (other_id,))
        job = self.store.claim(30)
        self.store.finish(job, Result(retryable=True, http_status=503), 0)
        exhausted = self.store.get("acme", identifier)
        self.assertEqual(exhausted["status"], "reconciliation_required")
        self.assertTrue(exhausted["outcome_uncertain"])

    def test_unsafe_supplier_crash_never_blindly_retries(self):
        registry = json.loads(self.credentials.read_text())
        registry["crm"]["retry_mode"] = "manual_on_ambiguous"
        self.credentials.write_text(json.dumps(registry))
        identifier = self.submit_job()
        job = self.store.claim(30)
        self.force_expire(identifier)
        self.assertIsNone(self.store.claim(30))
        result = self.store.get("acme", identifier)
        self.assertEqual(result["status"], "reconciliation_required")
        self.assertEqual(result["last_error"], "manual_reconciliation_required")
        self.assertFalse(self.store.finish(job, Result(success=True), 0))

    def test_reconciliation_closes_delivered_and_retries_only_confirmed_absence(self):
        registry = json.loads(self.credentials.read_text())
        registry["crm"]["retry_mode"] = "manual_on_ambiguous"
        self.credentials.write_text(json.dumps(registry))
        operator = Principal("ops", "acme", "operator")

        delivered = self.submit_job()
        job = self.store.claim(30)
        self.store.finish(job, Result(error="manual_reconciliation_required"), 0)
        self.assertEqual(self.store.get("acme", delivered)["status"], "reconciliation_required")
        self.assertEqual(
            self.store.reconcile(operator, delivered, "delivered", "INC-101", "vendor confirms order"),
            {
                "id": delivered, "status": "succeeded", "next_action": None,
                "retry_requested": False, "retry_scheduled": False,
            },
        )
        self.assertFalse(self.store.get("acme", delivered)["replayable"])
        history = self.store.get("acme", delivered)["reconciliations"]
        self.assertEqual(history[0]["outcome"], "delivered")
        self.assertEqual(history[0]["evidence_ref"], "INC-101")
        self.assertFalse(self.store.get("acme", delivered)["outcome_uncertain"])

        absent = self.submit_job()
        job = self.store.claim(30)
        self.store.finish(job, Result(error="manual_reconciliation_required"), 0)
        self.assertEqual(
            self.store.reconcile(operator, absent, "not_delivered", "INC-102", "vendor confirms absent"),
            {
                "id": absent, "status": "pending", "next_action": None,
                "retry_requested": True, "retry_scheduled": True,
            },
        )
        retry = self.store.claim(30)
        self.assertEqual(retry["id"], absent)
        self.assertEqual(retry["cycle_attempts"], 1)

    def test_reconciliation_api_requires_reconciler_and_structured_evidence(self):
        registry = json.loads(self.credentials.read_text())
        registry["crm"]["retry_mode"] = "manual_on_ambiguous"
        self.credentials.write_text(json.dumps(registry))
        identifier = self.submit_job()
        job = self.store.claim(30)
        self.store.finish(job, Result(error="manual_reconciliation_required"), 0)
        base_path = f"/v1/notifications/{identifier}"
        path = base_path + "/reconcile"
        operator = {"Authorization": "Bearer " + "o" * 40}
        reconciler = {"Authorization": "Bearer " + "r" * 40}
        self.assertEqual(self.client.post(path, headers=self.headers, json={
            "outcome": "delivered", "evidence_ref": "INC-1", "reason": "verified",
        }).status_code, 403)
        self.assertEqual(self.client.post(path, headers=operator, json={
            "outcome": "delivered", "evidence_ref": "INC-1", "reason": "verified",
        }).status_code, 403)
        self.assertEqual(self.client.post(base_path + "/replay", headers=reconciler, json={
            "reason": "not allowed",
        }).status_code, 403)
        self.assertEqual(self.client.post(path, headers=reconciler, json={
            "outcome": "delivered", "evidence_ref": "", "reason": "verified",
        }).status_code, 400)
        self.assertEqual(self.client.post(path, headers=reconciler, json={
            "outcome": "delivered", "evidence_ref": "INC-1", "reason": "verified",
            "retry_after_reconciliation": True,
        }).status_code, 400)
        response = self.client.post(path, headers=reconciler, json={
            "outcome": "delivered", "evidence_ref": "INC-123", "reason": "vendor confirms accepted",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "succeeded")
        self.assertFalse(response.json()["retry_scheduled"])

        capped = self.submit_job("api-reconcile-cap")
        claimed = self.store.claim(30)
        self.assertTrue(self.store.finish(
            claimed, Result(error="manual_reconciliation_required"), 0,
        ))
        with self.store.pool.connection() as conn:
            conn.execute(
                "UPDATE delivery_jobs SET replay_count=10 WHERE id=%s", (capped,),
            )
        response = self.client.post(path.replace(identifier, capped), headers=reconciler, json={
            "outcome": "not_delivered", "evidence_ref": "INC-CAP",
            "reason": "supplier confirms operation absent",
            "retry_after_reconciliation": True,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "failed")
        self.assertTrue(response.json()["retry_requested"])
        self.assertFalse(response.json()["retry_scheduled"])
        detail = self.client.get(
            f"/v1/notifications/{capped}", headers=self.headers,
        ).json()
        self.assertEqual(detail["reconciliations"][0]["outcome"], "not_delivered")
        self.assertFalse(detail["reconciliations"][0]["retry_scheduled"])
        self.assertIsNone(self.store.claim(30))
        self.assertEqual(self.client.post("/v1/notifications", headers=reconciler, json={
            **self.request,
        }).status_code, 403)

    def test_confirmed_non_delivery_after_supplier_key_expiry_requires_new_key(self):
        registry = json.loads(self.credentials.read_text())
        registry["crm"].update(retry_mode="idempotent", deduplication_window_seconds=3600)
        self.credentials.write_text(json.dumps(registry))
        request = {**self.request, "headers": {"Idempotency-Key": "operation-456"}}
        identifier = self.submit_job(request=request)
        job = self.store.claim(30)
        self.store.finish(job, Result(retryable=True, http_status=503), 0)
        with self.store.pool.connection() as conn:
            conn.execute(
                """UPDATE delivery_jobs SET key_expires_at=extract(epoch FROM clock_timestamp())+1
                WHERE id=%s""", (identifier,),
            )
        self.assertIsNone(self.store.claim(30))
        self.assertEqual(self.store.get("acme", identifier)["status"], "reconciliation_required")
        result = self.store.reconcile(
            Principal("reconciler", "acme", "reconciler"), identifier,
            "not_delivered", "INC-456", "supplier confirms no operation",
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["next_action"], "submit_new_operation_with_fresh_supplier_key")
        self.assertEqual(
            self.store.get("acme", identifier)["reconciliations"][0]["outcome"],
            "not_delivered",
        )

    def test_retained_count_and_byte_quotas_include_successful_jobs(self):
        identifier = self.submit_job("quota")
        job = self.store.claim(30)
        self.store.finish(job, Result(success=True), 0)
        count = self.store.stats()["retained_jobs"]
        size = self.store.stats()["snapshot_bytes"]
        self.assertGreater(size, 0)
        self.assertEqual(count, 1)
        self.settings.max_retained = 1
        with self.assertRaises(CapacityExceeded):
            self.submit_job()
        self.assertEqual(self.submit_job("quota"), identifier)
        self.settings.max_retained = 100
        self.settings.max_snapshot_bytes = size
        with self.assertRaises(CapacityExceeded):
            self.submit_job()
        with self.store.pool.connection() as conn:
            conn.execute("UPDATE delivery_jobs SET updated_at=extract(epoch FROM now())-31*86400")
        self.store.maintain()
        self.assertEqual(self.store.stats()["snapshot_bytes"], 0)
        self.assertEqual(self.store.stats()["retained_jobs"], 1)
        with self.store.pool.connection() as conn:
            conn.execute("UPDATE delivery_jobs SET updated_at=extract(epoch FROM now())-91*86400")
        self.store.maintain()
        self.assertEqual(self.store.stats()["retained_jobs"], 0)
        self.assertIsNone(self.store.get("acme", identifier))

    def test_expired_supplier_deduplication_window_blocks_replay(self):
        registry = json.loads(self.credentials.read_text())
        registry["crm"].update(retry_mode="idempotent", deduplication_window_seconds=3600)
        self.credentials.write_text(json.dumps(registry))
        request = {**self.request, "headers": {"Idempotency-Key": "operation-123"}}
        identifier = self.submit_job(request=request)
        item = self.store.get("acme", identifier)
        self.assertIsNone(item["key_expires_at"])
        job = self.store.claim(30)
        self.assertGreaterEqual(job["key_expires_at"] - job["deadline"], self.settings.timeout_seconds + 3)
        self.store.finish(job, Result(http_status=400), 0)
        with self.store.pool.connection() as conn:
            conn.execute(
                """UPDATE delivery_jobs SET key_expires_at=extract(epoch FROM clock_timestamp())+2
                WHERE id=%s""", (identifier,),
            )
        self.assertEqual(
            self.store.replay(Principal("ops", "acme", "operator"), identifier, "repair"),
            "reconciliation_required",
        )
        self.assertEqual(self.store.get("acme", identifier)["status"], "reconciliation_required")

    def test_live_timeout_contracts_control_duplicates_and_manual_intervention(self):
        from notifier.delivery import RetryPolicy
        from notifier.mock_supplier import supplier_server
        from notifier.worker import Worker

        supplier = supplier_server(port=0)
        supplier.start()
        self.settings.origins = (supplier.url,)
        self.settings.development = True
        self.credentials.write_text(json.dumps({
            "legacy": {"supplier_account": "mock-legacy", "tenants": ["acme"],
                       "origins": [supplier.url],
                       "operations": [{"method": "POST", "path": "/vendor/legacy-orders",
                                       "query": {"scenario": ["timeout_once"]}}],
                       "headers": {"X-API-Key": "supplier-demo-secret"},
                       "retry_mode": "manual_on_ambiguous"},
            "safe": {"supplier_account": "mock-safe", "tenants": ["acme"],
                     "origins": [supplier.url],
                     "operations": [{"method": "POST", "path": "/vendor/orders",
                                     "query": {"scenario": ["timeout_once"]}}],
                     "headers": {"X-API-Key": "supplier-demo-secret"},
                     "retry_mode": "idempotent", "deduplication_window_seconds": 86400},
        }))
        self.security = security_for(self.settings)
        self.store.security = self.security
        worker = Worker(self.store, SupplierTransport(self.security, .1),
                        RetryPolicy(.01, .01, 0), lease_seconds=30)
        try:
            manual = self.submit_job(request={
                "url": supplier.url + "/vendor/legacy-orders?scenario=timeout_once",
                "headers": {"Content-Type": "application/json"},
                "body": '{"order_id":"unsafe-live","amount_cents":100}', "credential_ref": "legacy",
            })
            self.assertTrue(worker.run_once())
            self.assertEqual(self.store.get("acme", manual)["last_error"], "manual_reconciliation_required")
            self.assertEqual(self.store.get("acme", manual)["status"], "reconciliation_required")
            safe = self.submit_job(request={
                "url": supplier.url + "/vendor/orders?scenario=timeout_once",
                "headers": {"Content-Type": "application/json", "Idempotency-Key": "safe-live-key"},
                "body": '{"order_id":"safe-live","amount_cents":100}', "credential_ref": "safe",
            })
            self.assertTrue(worker.run_once())
            self.assertEqual(self.store.get("acme", safe)["status"], "pending")
            time.sleep(.03)
            self.assertTrue(worker.run_once())
            self.assertEqual(self.store.get("acme", safe)["status"], "succeeded")
            state = supplier.state.snapshot()
            self.assertEqual(state["calls"]["unsafe-live"], 1)
            self.assertEqual(state["calls"]["safe-live"], 2)
            self.assertEqual(state["effects"], {"unsafe-live": 1, "safe-live": 1})
        finally:
            supplier.stop()
