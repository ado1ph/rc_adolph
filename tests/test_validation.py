import socket
import time
import unittest
from unittest.mock import Mock, patch

from notifier.delivery import (
    ExecutionControl,
    HttpTransport,
    PinnedConnection,
    RetryPolicy,
    retry_after_seconds,
)
from notifier.validation import (
    InvalidRequest,
    TargetPolicy,
    notification_request,
    split_target,
    strict_json,
)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.policy = TargetPolicy(["https://vendor.example"])

    def test_rejects_ambiguous_or_unsafe_urls(self):
        for url in [
            "file:///etc/passwd", "https://user:pass@vendor.example/api",
            "https://vendor.example/api#secret", "https://vendor.example:0/api",
            "https://vendor.example:70000/api", "https://vendor.example\n/api",
            "https://vendor.example./api", "https://[::1%25en0]/api",
            "https://vendor.example\\@internal/api",
        ]:
            with self.subTest(url=url), self.assertRaises(InvalidRequest):
                split_target(url)

    def test_allowlist_is_exact_origin_not_suffix_or_path_match(self):
        for url in [
            "https://vendor.example.attacker.test", "https://vendor.example:8443",
            "http://vendor.example", "https://other.example",
        ]:
            with self.subTest(url=url), self.assertRaises(InvalidRequest):
                self.policy.validate(url)
        self.policy.validate("https://vendor.example:443/a?x=1")

    def test_dns_private_mixed_and_multicast_results_are_blocked(self):
        for ips in [
            ["127.0.0.1"], ["169.254.169.254"], ["10.0.0.1"],
            ["93.184.216.34", "192.168.0.1"], ["224.0.0.1"], ["::ffff:127.0.0.1"],
        ]:
            answers = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips
            ]
            with self.subTest(ips=ips), patch(
                "socket.getaddrinfo", return_value=answers
            ), self.assertRaises(InvalidRequest):
                self.policy.resolve("https://vendor.example")

    def test_public_dns_result_is_pinned(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("socket.getaddrinfo", return_value=answers):
            self.assertEqual(self.policy.resolve("https://vendor.example/a")[2], "93.184.216.34")

    def test_http_loopback_requires_explicit_demo_flag_and_allowlist(self):
        policy = TargetPolicy(["http://127.0.0.1:9100"])
        with self.assertRaises(InvalidRequest):
            policy.validate("http://127.0.0.1:9100/a")
        policy = TargetPolicy(["http://127.0.0.1:9100"], allow_loopback=True)
        self.assertEqual(policy.resolve("http://127.0.0.1:9100/a")[2], "127.0.0.1")
        with self.assertRaises(InvalidRequest):
            policy.validate("http://127.0.0.1:9101/a")

    def test_header_injection_hop_by_hop_and_duplicates_are_rejected(self):
        for headers in [
            {"Host": "attacker.test"}, {"Content-Length": "100"}, {"Expect": "100-continue"},
            {"X-Key": "secret\r\nHost: attacker"}, {"invalid name": "x"},
            {"X-Key": "a", "x-key": "b"}, {"X-Key": 42},
        ]:
            with self.subTest(headers=headers), self.assertRaises(InvalidRequest):
                notification_request({"url": "https://vendor.example", "headers": headers}, self.policy)

    def test_normalized_headers_are_idempotent_but_body_is_not_reformatted(self):
        first = {"url": "https://vendor.example", "headers": {"X-Key": "x"}, "body": '{"x":1}'}
        second = {"url": "https://vendor.example", "headers": {"x-key": "x"}, "body": '{"x":1}'}
        self.assertEqual(
            notification_request(first, self.policy)[1],
            notification_request(second, self.policy)[1],
        )
        second["body"] = '{"x": 1}'
        self.assertNotEqual(
            notification_request(first, self.policy)[1],
            notification_request(second, self.policy)[1],
        )

    def test_invalid_payloads_are_rejected(self):
        for value in [
            [], {}, {"url": "https://vendor.example", "body": {}},
            {"url": "https://vendor.example", "body": "x" * (128 * 1024 + 1)},
            {"url": "https://vendor.example", "body": "\ud800"},
            {"url": "https://vendor.example", "method": "CONNECT"},
            {"url": "https://vendor.example", "unexpected": True},
        ]:
            with self.subTest(value_type=type(value)), self.assertRaises(InvalidRequest):
                notification_request(value, self.policy)

    def test_strict_json_rejects_nonfinite_and_duplicate_keys(self):
        for payload in ['{"x":NaN}', '{"x":Infinity}', '{"x":1,"x":2}', b"\xff"]:
            with self.subTest(payload=payload), self.assertRaises(InvalidRequest):
                strict_json(payload)

    def test_retry_after_and_bounded_backoff(self):
        self.assertEqual(retry_after_seconds("10"), 10)
        self.assertEqual(retry_after_seconds(" \t7200\t "), 7200)
        self.assertEqual(retry_after_seconds("7200"), 7200)
        self.assertEqual(retry_after_seconds("999999"), 999999)
        self.assertIsNone(retry_after_seconds("999999999"))
        self.assertIsNone(retry_after_seconds("9" * 5000))
        self.assertEqual(retry_after_seconds("-1"), 0)
        self.assertEqual(retry_after_seconds("not-a-date"), 0)
        self.assertEqual(retry_after_seconds("Thu, 01 Jan 1970 00:00:10 GMT", now=5), 5)
        self.assertEqual(retry_after_seconds(" \tThu, 01 Jan 1970 00:00:10 GMT\t ", now=5), 5)
        retry = RetryPolicy(base_seconds=2, cap_seconds=60, jitter_ratio=0)
        self.assertEqual([retry.delay(i) for i in (1, 2, 3, 100)], [2, 4, 8, 60])
        self.assertEqual(retry.delay(1, retry_after=120), 120)

    def test_oversized_retry_after_is_a_finite_cooldown_signal(self):
        value = retry_after_seconds("9" * 5000)
        self.assertIsNone(value)
        self.assertFalse(isinstance(value, float))

    def test_tls_keeps_original_hostname_while_socket_uses_validated_ip(self):
        connection = PinnedConnection("vendor.example", 443, "93.184.216.34", 3, True)
        connection.context = Mock()
        with patch("socket.create_connection") as connect:
            connection.connect()
        connect.assert_called_once_with(("93.184.216.34", 443), 3)
        connection.context.wrap_socket.assert_called_once_with(
            connect.return_value, server_hostname="vendor.example"
        )
        connection.close()

    def test_execution_control_blocks_expired_or_lost_lease(self):
        import time

        control = ExecutionControl(time.monotonic() - 1)
        with self.assertRaises(TimeoutError):
            control.remaining()

    def test_transport_does_not_connect_after_dns_consumes_execution_budget(self):
        import time
        from urllib.parse import urlsplit

        from notifier.delivery import ExecutionControl

        class SlowPolicy:
            def resolve(self, _url, timeout=None):
                time.sleep(.03)
                return urlsplit("https://vendor.example/path"), (
                    "https", "vendor.example", 443,
                ), "93.184.216.34"

        control = ExecutionControl(time.monotonic() + .01)
        with patch("socket.create_connection") as connect:
            result = HttpTransport(SlowPolicy(), timeout=1).send({
                "url": "https://vendor.example/path", "method": "POST",
                "body": "", "headers": {},
            }, control=control)
        self.assertEqual(result.error, "execution_budget_exhausted")
        connect.assert_not_called()
        control = ExecutionControl()
        control.lease_lost.set()
        with self.assertRaises(TimeoutError):
            control.remaining()

    def test_connect_and_tls_handshake_timeouts_are_retryable_before_send(self):
        import time
        from urllib.parse import urlsplit

        class Policy:
            def resolve(self, _url, timeout=None):
                return urlsplit("https://vendor.example/path"), (
                    "https", "vendor.example", 443,
                ), "93.184.216.34"

        request = {
            "url": "https://vendor.example/path", "method": "POST",
            "body": "{}", "headers": {},
        }

        def consume_timeout(_address, timeout):
            time.sleep(timeout + .01)
            raise TimeoutError("connect timed out")

        with patch(
            "notifier.delivery.socket.create_connection",
            side_effect=consume_timeout,
        ):
            connect_result = HttpTransport(Policy(), timeout=.03).send(
                request, control=ExecutionControl(time.monotonic() + 10),
            )
        socket_obj = Mock()
        context = Mock()
        def consume_tls_timeout(_socket, server_hostname):
            time.sleep(socket_obj.timeout + .01)
            raise TimeoutError("TLS timed out")

        socket_obj.settimeout.side_effect = lambda timeout: setattr(socket_obj, "timeout", timeout)
        context.wrap_socket.side_effect = consume_tls_timeout
        with patch("notifier.delivery.socket.create_connection", return_value=socket_obj):
            with patch("notifier.delivery.ssl.create_default_context", return_value=context):
                tls_result = HttpTransport(Policy(), timeout=.03).send(
                    request, control=ExecutionControl(time.monotonic() + 10),
                )
        for result in (connect_result, tls_result):
            self.assertEqual(result.error, "network_timeout")
            self.assertTrue(result.retryable)
            self.assertFalse(result.ambiguous)
            self.assertEqual(result.failure_class, "supplier_network")

    def test_abort_during_response_wait_is_classified_as_timeout(self):
        import http.client
        from urllib.parse import urlsplit

        class Policy:
            def resolve(self, _url, timeout=None):
                return urlsplit("http://vendor.example/path"), (
                    "http", "vendor.example", 80,
                ), "93.184.216.34"

        class FakeTimer:
            current = None

            def __init__(self, _interval, function):
                self.function = function
                FakeTimer.current = self

            def start(self):
                pass

            def cancel(self):
                pass

        class AbortedConnection:
            failure = None
            abort_action = None

            def __init__(self, *_args, **_kwargs):
                self.on_write = None
                self.control = None

            def connect(self):
                pass

            def request(self, *_args, **_kwargs):
                self.on_write()

            def getresponse(self):
                FakeTimer.current.function()
                raise AbortedConnection.failure

            def abort(self):
                if AbortedConnection.abort_action is not None:
                    AbortedConnection.abort_action(self.control)

            def close(self):
                pass

        request = {
            "url": "http://vendor.example/path", "method": "POST",
            "body": "{}", "headers": {},
        }

        def send(failure, control=None, abort_action=None):
            AbortedConnection.failure = failure
            AbortedConnection.abort_action = abort_action
            with patch("notifier.delivery.threading.Timer", FakeTimer):
                with patch("notifier.delivery.PinnedConnection", AbortedConnection):
                    return HttpTransport(Policy(), timeout=30).send(
                        request, control=control,
                    )

        for failure in (OSError(9, "Bad file descriptor"), http.client.RemoteDisconnected()):
            with self.subTest(failure=type(failure).__name__):
                result = send(failure)
                self.assertEqual(result.error, "ambiguous_network_timeout")
                self.assertFalse(result.retryable)
                self.assertTrue(result.ambiguous)
                self.assertEqual(result.request_state, "possibly_sent")
                self.assertEqual(result.failure_phase, "response")

        lease = ExecutionControl()
        lease_result = send(
            OSError(9, "Bad file descriptor"), control=lease,
            abort_action=lambda control: control.mark_lease_lost(),
        )
        self.assertEqual(lease_result.error, "lease_lost")
        self.assertEqual(lease_result.failure_class, "local_policy")

        deadline = ExecutionControl(time.monotonic() + 10)
        deadline_result = send(
            OSError(9, "Bad file descriptor"), control=deadline,
            abort_action=lambda control: setattr(
                control, "deadline", time.monotonic() - 1,
            ),
        )
        self.assertEqual(deadline_result.error, "execution_budget_exhausted")
        self.assertEqual(deadline_result.failure_class, "local_policy")

    def test_budget_exhaustion_and_lease_loss_are_not_supplier_failures(self):
        from urllib.parse import urlsplit

        class Policy:
            def resolve(self, _url, timeout=None):
                return urlsplit("https://vendor.example/path"), (
                    "https", "vendor.example", 443,
                ), "93.184.216.34"

        request = {
            "url": "https://vendor.example/path", "method": "POST",
            "body": "{}", "headers": {},
        }
        with patch("notifier.delivery.PinnedConnection.connect") as connect:
            expired = HttpTransport(Policy(), timeout=2).send(
                request, control=ExecutionControl(time.monotonic() - 1),
            )
        self.assertEqual(expired.error, "execution_budget_exhausted")
        self.assertFalse(expired.retryable)
        self.assertEqual(expired.failure_class, "local_policy")
        connect.assert_not_called()

        control = ExecutionControl(time.monotonic() + 10)
        control.mark_lease_lost()
        with patch("notifier.delivery.PinnedConnection.connect") as connect:
            lost = HttpTransport(Policy(), timeout=2).send(request, control=control)
        self.assertEqual(lost.error, "lease_lost")
        self.assertFalse(lost.retryable)
        self.assertEqual(lost.failure_class, "local_policy")
        connect.assert_not_called()

    def test_transport_budget_is_rechecked_immediately_before_request(self):
        from urllib.parse import urlsplit

        class Policy:
            def resolve(self, _url, timeout=None):
                return urlsplit("http://vendor.example/path"), (
                    "http", "vendor.example", 80,
                ), "93.184.216.34"

        request = {
            "url": "http://vendor.example/path", "method": "POST",
            "body": "{}", "headers": {},
        }
        with patch(
            "notifier.delivery.PinnedConnection.connect", side_effect=lambda: time.sleep(.04),
        ):
            with patch("notifier.delivery.PinnedConnection.request") as send_request:
                result = HttpTransport(Policy(), timeout=.01).send(request)
        self.assertEqual(result.error, "execution_budget_exhausted")
        self.assertEqual(result.request_state, "not_sent")
        self.assertFalse(result.retryable)
        send_request.assert_not_called()

    def test_lease_loss_signal_is_not_blocked_by_an_active_outbound_guard(self):
        import threading

        control = ExecutionControl()
        inside_guard = threading.Event()
        release_guard = threading.Event()

        def hold_guard():
            with control.outbound_guard():
                inside_guard.set()
                release_guard.wait(1)

        sender = threading.Thread(target=hold_guard)
        sender.start()
        self.assertTrue(inside_guard.wait(1))
        control.mark_lease_lost()
        self.assertTrue(control.lease_lost.is_set())
        release_guard.set()
        sender.join(1)
        self.assertFalse(sender.is_alive())
        with self.assertRaises(TimeoutError):
            with control.outbound_guard():
                pass

    def test_partial_send_and_response_loss_are_ambiguous(self):
        from urllib.parse import urlsplit

        class Policy:
            def resolve(self, _url, timeout=None):
                return urlsplit("http://vendor.example/path"), (
                    "http", "vendor.example", 80,
                ), "93.184.216.34"

        request = {
            "url": "http://vendor.example/path", "method": "POST",
            "body": "{}", "headers": {},
        }

        class PartialSocket:
            def settimeout(self, _timeout):
                pass

            def sendall(self, data):
                self.partial = data[:5]
                raise TimeoutError("partial write")

            def close(self):
                pass

            def shutdown(self, _how):
                pass

        partial = PartialSocket()
        with patch("notifier.delivery.socket.create_connection", return_value=partial):
            result = HttpTransport(Policy(), timeout=2).send(request)
        self.assertEqual(result.failure_phase, "write")
        self.assertEqual(result.request_state, "possibly_sent")
        self.assertTrue(result.ambiguous)
        self.assertFalse(result.retryable)

        sent = PartialSocket()
        sent.sendall = Mock()
        with patch("notifier.delivery.socket.create_connection", return_value=sent):
            with patch(
                "notifier.delivery.PinnedConnection.getresponse",
                side_effect=TimeoutError("response lost"),
            ):
                result = HttpTransport(Policy(), timeout=2).send(request)
        self.assertEqual(result.failure_phase, "response")
        self.assertEqual(result.request_state, "possibly_sent")
        self.assertTrue(result.ambiguous)
        self.assertFalse(result.retryable)

    def test_lease_lost_after_connection_but_before_send_sends_no_bytes(self):
        from urllib.parse import urlsplit

        class Policy:
            def resolve(self, _url, timeout=None):
                return urlsplit("http://vendor.example/path"), (
                    "http", "vendor.example", 80,
                ), "93.184.216.34"

        class Socket:
            def settimeout(self, _timeout):
                pass

            def sendall(self, _data):
                self.sent = True

            def close(self):
                pass

        sock = Socket()
        control = ExecutionControl(time.monotonic() + 10)

        def lose_lease(connection):
            connection.sock = sock
            control.mark_lease_lost()

        with patch("notifier.delivery.socket.create_connection", return_value=sock):
            with patch("notifier.delivery.PinnedConnection.connect", lose_lease):
                result = HttpTransport(Policy(), timeout=2).send({
                    "url": "http://vendor.example/path", "method": "POST",
                    "body": "{}", "headers": {},
                }, control=control)
        self.assertEqual(result.error, "lease_lost")
        self.assertFalse(result.retryable)
        self.assertFalse(getattr(sock, "sent", False))

    def test_lease_lost_while_waiting_for_response_keeps_ambiguous_outcome(self):
        from urllib.parse import urlsplit

        class Policy:
            def resolve(self, _url, timeout=None):
                return urlsplit("http://vendor.example/path"), (
                    "http", "vendor.example", 80,
                ), "93.184.216.34"

        class Socket:
            def settimeout(self, _timeout):
                pass

            def sendall(self, _data):
                pass

            def close(self):
                pass

        control = ExecutionControl(time.monotonic() + 10)
        def lost_response(_connection):
            control.mark_lease_lost()
            raise TimeoutError("response interrupted")

        with patch("notifier.delivery.socket.create_connection", return_value=Socket()):
            with patch(
                "notifier.delivery.PinnedConnection.getresponse", lost_response,
            ):
                result = HttpTransport(Policy(), timeout=2).send({
                    "url": "http://vendor.example/path", "method": "POST",
                    "body": "{}", "headers": {},
                }, control=control)
        self.assertEqual(result.error, "lease_lost")
        self.assertEqual(result.request_state, "possibly_sent")
        self.assertTrue(result.ambiguous)
        self.assertFalse(result.retryable)
        self.assertEqual(result.failure_class, "local_policy")
