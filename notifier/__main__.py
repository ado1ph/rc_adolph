import argparse
import logging
import math
import os
import signal
import threading

from .api import api_server
from .delivery import HttpTransport, RetryPolicy
from .mock_supplier import supplier_server
from .store import Store
from .validation import TargetPolicy
from .worker import Worker, WorkerPool


def main():
    parser = argparse.ArgumentParser(description="Single-node reliable HTTP notifications")
    parser.add_argument("command", choices=["serve", "supplier", "demo"])
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.command == "demo":
        from .demo import run_demo
        run_demo()
        return
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stopped.set())
    pool = None
    if args.command == "supplier":
        server = supplier_server(args.port if args.port is not None else 9100)
    else:
        token = os.environ.get("NOTIFY_TOKEN", "")
        if not token:
            parser.error("Set NOTIFY_TOKEN; no default production credentials are provided")
        try:
            timeout = float(os.environ.get("NOTIFY_HTTP_TIMEOUT", "3"))
            count = int(os.environ.get("NOTIFY_WORKERS", "2"))
            attempts = int(os.environ.get("NOTIFY_MAX_ATTEMPTS", "5"))
            if not math.isfinite(timeout) or not 0.05 <= timeout <= 30:
                raise ValueError("HTTP timeout must be between 0.05 and 30 seconds")
            if not 1 <= count <= 16:
                raise ValueError("Worker count must be between 1 and 16")
            origins = list(filter(None, os.environ.get("NOTIFY_ALLOWED_ORIGINS", "").split(",")))
            policy = TargetPolicy(
                origins, allow_loopback=os.environ.get("NOTIFY_ALLOW_LOOPBACK") == "1"
            )
            if not policy.origins:
                parser.error("Set NOTIFY_ALLOWED_ORIGINS; an empty allowlist accepts no targets")
            store = Store(os.environ.get("NOTIFY_DB", ".data/notifications.sqlite3"))
            server = api_server(
                store, policy, token, args.port if args.port is not None else 9000, attempts
            )
            worker = Worker(
                store, HttpTransport(policy, timeout), RetryPolicy(),
                lease_seconds=max(10, timeout * 3),
            )
            pool = WorkerPool(worker, count=count)
        except ValueError as exc:
            parser.error(str(exc))
    try:
        server.start()
        if pool:
            pool.start()
        print("{} listening at {} (loopback only)".format(args.command, server.url), flush=True)
        stopped.wait()
    finally:
        server.stop()
        if pool:
            pool.stop()


if __name__ == "__main__":
    main()
