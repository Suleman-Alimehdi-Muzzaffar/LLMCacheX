"""Phase 5 concurrency tests.

Scope: local multi-threaded use of one process. These tests detect obvious
race conditions in SQLite access, rate-limiter state and cache reads/writes.

Known limitations (documented, not hidden):
- SQLiteStorage serializes operations with an internal lock: safe for
  threads sharing one instance. It is NOT multi-process safe; separate
  processes rely on SQLite's own file locking only.
- A cache miss executed by several threads at once may execute the wrapped
  function more than once (no request coalescing / single-flight).
"""

import sqlite3
import threading
import time

from llmcachex import cached_call
from llmcachex.resilience import RateLimiter
from llmcachex.storage import SQLiteStorage


def test_concurrent_threads_can_share_one_storage_instance(tmp_path):
    storage = SQLiteStorage(tmp_path / "shared.db")
    errors = []

    def worker(index: int) -> None:
        try:
            for i in range(10):
                storage.set(f"key-{index}-{i}", {"index": index, "i": i})
                assert storage.get(f"key-{index}-{i}") == {
                    "index": index,
                    "i": i,
                }
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    try:
        threads = [
            threading.Thread(target=worker, args=(n,)) for n in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors
        assert storage.exists("key-0-0")
        assert storage.exists("key-5-9")
    finally:
        storage.close()


def test_concurrent_decorated_calls_stay_consistent(tmp_path):
    @cached_call(cache_path=tmp_path / "conc.db")
    def compute(value):
        return value * 2

    results: list[int | None] = [None] * 12
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            results[index] = compute(index % 4)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    # Every thread got a correct result (executions may repeat across
    # threads: single-flight coalescing is not implemented).
    assert results == [(n % 4) * 2 for n in range(12)]

    # The database must remain readable and internally consistent.
    with sqlite3.connect(tmp_path / "conc.db") as con:
        rows = con.execute(
            "SELECT request_hash, response_json FROM cache_entries"
        ).fetchall()
    assert rows
    for _, response_json in rows:
        assert isinstance(response_json, str)
        import json

        json.loads(response_json)  # valid JSON for every row


def test_concurrent_reads_after_writes_do_not_corrupt(tmp_path):
    db = tmp_path / "rw.db"
    writer = SQLiteStorage(db)
    reader = SQLiteStorage(db)
    errors: list[BaseException] = []

    def write() -> None:
        try:
            for i in range(20):
                writer.set(f"w{i}", {"i": i})
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def read() -> None:
        try:
            for i in range(20):
                reader.get(f"w{i}")  # None or value; never an error
                reader.exists(f"w{i}")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    try:
        threads = [
            threading.Thread(target=write),
            threading.Thread(target=read),
            threading.Thread(target=read),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors
        assert reader.exists("w19")
    finally:
        writer.close()
        reader.close()


def test_rate_limiter_under_concurrency_respects_window():
    limiter = RateLimiter(4, 0.4)
    errors: list[BaseException] = []
    start_times: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            limiter.acquire()
            with lock:
                start_times.append(time.monotonic())
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    started = time.monotonic()
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert len(start_times) == 4
    # All 4 fit in the first window: no thread had to wait a full period.
    assert time.monotonic() - started < 0.35
