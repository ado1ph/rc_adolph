import logging
import threading

from .delivery import ExecutionControl, RetryPolicy

LOGGER = logging.getLogger(__name__)


class Worker:
    def __init__(self, store, transport, retry=None, lease_seconds=30):
        self.store = store
        self.transport = transport
        self.retry = retry or RetryPolicy()
        self.lease_seconds = lease_seconds

    def run_once(self):
        job = self.store.claim(self.lease_seconds)
        if job is None:
            return False
        done = threading.Event()
        control = ExecutionControl(job.get("execution_deadline_monotonic"))

        def renew():
            while not done.wait(self.lease_seconds / 3):
                try:
                    if not self.store.heartbeat(job, self.lease_seconds):
                        LOGGER.warning("Lease lost for notification %s", job["id"])
                        control.mark_lease_lost()
                        return
                except Exception:
                    LOGGER.error("Lease renewal failed for notification %s", job["id"])
                    control.mark_lease_lost()
                    return

        heartbeat = threading.Thread(target=renew, name="lease-heartbeat", daemon=True)
        heartbeat.start()
        try:
            control.remaining()
            result = self.transport.send(job["request"], control=control)
            delay = self.retry.delay(job["cycle_attempts"], result.retry_after)
            if not self.store.finish(job, result, delay):
                LOGGER.warning("Discarded stale result for notification %s", job["id"])
        finally:
            done.set()
            heartbeat.join()
        return True


class WorkerPool:
    def __init__(self, worker, count=2, poll_seconds=0.2):
        self.worker = worker
        self.count = count
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()
        self.threads = []

    def start(self):
        def loop():
            while not self.stop_event.is_set():
                try:
                    if self.worker.run_once():
                        continue
                except Exception:
                    # Leave the in-flight record for lease-based recovery, not immediate duplication.
                    LOGGER.error("Worker iteration failed; durable lease will recover the job")
                self.stop_event.wait(self.poll_seconds)

        for number in range(self.count):
            thread = threading.Thread(target=loop, name="delivery-" + str(number), daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=self.worker.lease_seconds + 1)
