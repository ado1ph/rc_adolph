import argparse
import logging
import os
import signal
import threading
import time
import uuid

import uvicorn

from ..delivery import RetryPolicy
from ..worker import Worker, WorkerPool
from .config import Settings
from .runtime import SupplierTransport, configure_logging, security_for
from .store import PostgresStore

LOGGER = logging.getLogger(__name__)


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("api", "worker", "migrate", "maintain", "rotate"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    configure_logging()
    settings = Settings.from_env()
    security = security_for(settings)
    if args.command == "migrate":
        PostgresStore.migrate(settings.dsn)
        print("Schema migration complete")
        return
    if args.command == "api":
        from .api import create_app
        uvicorn.run(
            create_app(settings, security=security), host=args.host, port=args.port,
            access_log=False, proxy_headers=False, limit_concurrency=128,
            backlog=256, timeout_keep_alive=5, timeout_graceful_shutdown=35,
            server_header=False,
        )
        return
    store = PostgresStore(settings, security)
    store.open()
    try:
        if not store.ready():
            raise RuntimeError("Migration required")
        if args.command == "maintain":
            print("Records cleaned:", store.maintain())
            return
        if args.command == "rotate":
            print("Snapshots rotated:", store.rotate_snapshots())
            return
        stop = threading.Event()
        for name in (signal.SIGINT, signal.SIGTERM):
            signal.signal(name, lambda *_: stop.set())
        identifier = f"{os.getpid()}-{uuid.uuid4().hex}"
        worker = Worker(
            store, SupplierTransport(security, settings.timeout_seconds),
            RetryPolicy(base_seconds=10, cap_seconds=3600, jitter_ratio=.5),
            settings.lease_seconds,
        )
        pool = WorkerPool(worker, settings.workers)
        store.worker_seen(identifier)
        pool.start()
        maintenance_at = 0
        try:
            while not stop.wait(5):
                try:
                    store.worker_seen(identifier)
                    if time.monotonic() >= maintenance_at:
                        store.maintain()
                        maintenance_at = time.monotonic() + 60
                except Exception:
                    LOGGER.error("worker_control_tick_failed")
        finally:
            pool.stop()
    finally:
        store.close()


if __name__ == "__main__":
    run()
