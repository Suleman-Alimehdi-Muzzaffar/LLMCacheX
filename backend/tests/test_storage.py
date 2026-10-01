"""Tests for SQLiteStorage, including Phase 4 TTL expiration."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from llmcachex.storage import SQLiteStorage


def test_set_get_round_trip(tmp_path):
    storage = SQLiteStorage(tmp_path / "cache.db")
    try:
        storage.set("h1", {"message": "hello", "value": 123})
        assert storage.get("h1") == {"message": "hello", "value": 123}
        assert storage.exists("h1") is True
    finally:
        storage.close()


def test_missing_key_returns_none(tmp_path):
    storage = SQLiteStorage(tmp_path / "cache.db")
    try:
        assert storage.get("nope") is None
        assert storage.exists("nope") is False
    finally:
        storage.close()


def test_set_updates_existing_entry_without_duplicates(tmp_path):
    db = tmp_path / "cache.db"
    storage = SQLiteStorage(db)
    try:
        storage.set("h1", {"v": 1})
        storage.set("h1", {"v": 2})
        assert storage.get("h1") == {"v": 2}
        with sqlite3.connect(db) as con:
            count = con.execute(
                "SELECT COUNT(*) FROM cache_entries WHERE request_hash = ?",
                ("h1",),
            ).fetchone()[0]
        assert count == 1
    finally:
        storage.close()


def test_schema_has_expected_columns(tmp_path):
    db = tmp_path / "cache.db"
    storage = SQLiteStorage(db)
    try:
        with sqlite3.connect(db) as con:
            columns = {
                row[1]: row[2]
                for row in con.execute("PRAGMA table_info(cache_entries)")
            }
        assert columns["id"] == "INTEGER"
        assert columns["request_hash"] == "TEXT"
        assert columns["response_json"] == "TEXT"
        assert columns["created_at"] == "TEXT"
        assert columns["expires_at"] == "TEXT"
    finally:
        storage.close()


def test_migration_adds_expires_at_to_legacy_database(tmp_path):
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE cache_entries ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "request_hash TEXT UNIQUE NOT NULL, "
            "response_json TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO cache_entries "
            "(request_hash, response_json, created_at) VALUES (?, ?, ?)",
            ("old", '{"a": 1}', datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    storage = SQLiteStorage(db)
    try:
        # Legacy row has no expiry, so it stays readable.
        assert storage.get("old") == {"a": 1}
        storage.set("new", [1, 2, 3])
        assert storage.get("new") == [1, 2, 3]
    finally:
        storage.close()


def test_entry_with_future_expiry_is_returned(tmp_path):
    storage = SQLiteStorage(tmp_path / "cache.db")
    try:
        storage.set(
            "h1",
            "value",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        assert storage.get("h1") == "value"
        assert storage.exists("h1") is True
    finally:
        storage.close()


def test_expired_entry_behaves_as_miss(tmp_path):
    storage = SQLiteStorage(tmp_path / "cache.db")
    try:
        storage.set(
            "h1",
            "stale",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        assert storage.get("h1") is None
        assert storage.exists("h1") is False
    finally:
        storage.close()


def test_set_rejects_non_datetime_expires_at(tmp_path):
    storage = SQLiteStorage(tmp_path / "cache.db")
    try:
        with pytest.raises(TypeError):
            storage.set("h1", "x", expires_at="tomorrow")  # type: ignore[arg-type]
    finally:
        storage.close()


def test_clear_and_repeated_close(tmp_path):
    storage = SQLiteStorage(tmp_path / "cache.db")
    try:
        storage.set("h1", 1)
        storage.clear()
        assert storage.exists("h1") is False
    finally:
        storage.close()
        storage.close()


def test_database_file_created_automatically(tmp_path):
    db = tmp_path / "auto" / "cache.db"
    assert not db.exists()
    storage = SQLiteStorage(db)
    try:
        assert db.exists()
    finally:
        storage.close()


def test_persistence_across_close_and_reopen(tmp_path):
    db = tmp_path / "persist.db"
    first = SQLiteStorage(db)
    first.set("h1", {"persisted": [1, 2, {"x": None}]})
    first.set("h2", "second")
    first.close()

    second = SQLiteStorage(db)
    try:
        assert second.get("h1") == {"persisted": [1, 2, {"x": None}]}
        assert second.get("h2") == "second"
        assert second.exists("h1") and second.exists("h2")
    finally:
        second.close()


def test_multiple_distinct_hashes_coexist(tmp_path):
    storage = SQLiteStorage(tmp_path / "cache.db")
    try:
        expected = {f"h{i}": {"value": i} for i in range(5)}
        for key, value in expected.items():
            storage.set(key, value)
        for key, value in expected.items():
            assert storage.get(key) == value
        storage.clear()
        for key in expected:
            assert storage.exists(key) is False
    finally:
        storage.close()


def test_ttl_entries_coexist_with_non_expiring_entries(tmp_path):
    storage = SQLiteStorage(tmp_path / "cache.db")
    try:
        storage.set("forever", "no-expiry")
        storage.set(
            "brief",
            "expires",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        storage.set(
            "gone",
            "stale",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        assert storage.get("forever") == "no-expiry"
        assert storage.get("brief") == "expires"
        assert storage.get("gone") is None
        # Expired row still physically present (not auto-deleted).
        with sqlite3.connect(tmp_path / "cache.db") as con:
            count = con.execute(
                "SELECT COUNT(*) FROM cache_entries"
            ).fetchone()[0]
        assert count == 3
    finally:
        storage.close()


def test_list_entries_returns_metadata_without_payloads(tmp_path):
    storage = SQLiteStorage(tmp_path / "cache.db")
    try:
        storage.set("b", {"secret": "value"})
        storage.set(
            "a",
            [1, 2, 3],
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        entries = storage.list_entries()
        # Newest first: "a" was inserted last.
        assert [e.request_hash for e in entries] == ["a", "b"]
        assert entries[0].request_hash == "a"
        assert entries[0].expired is True
        assert entries[0].expires_at is not None
        assert entries[1].request_hash == "b"
        assert entries[1].expired is False
        assert entries[1].expires_at is None
        assert isinstance(entries[0].created_at, str)
        # CacheEntryMeta deliberately has no response field.
        assert not hasattr(entries[0], "response")
    finally:
        storage.close()


def test_list_entries_empty(tmp_path):
    storage = SQLiteStorage(tmp_path / "cache.db")
    try:
        assert storage.list_entries() == []
    finally:
        storage.close()
