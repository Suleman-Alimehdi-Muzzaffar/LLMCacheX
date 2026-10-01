"""Tests for deterministic request hashing (Phase 2, re-verified in Phase 4)."""

import pytest

from llmcachex.hashing import hash_request


def test_same_dict_different_key_order_same_hash():
    assert hash_request({"a": 1, "b": 2}) == hash_request({"b": 2, "a": 1})


def test_nested_dict_order_does_not_matter():
    first = {"request": {"model": "t", "config": {"temperature": 0.7, "top_p": 0.9}}}
    second = {"request": {"config": {"top_p": 0.9, "temperature": 0.7}, "model": "t"}}
    assert hash_request(first) == hash_request(second)


def test_changed_value_changes_hash():
    assert hash_request({"a": 1, "b": 2}) != hash_request({"a": 1, "b": 3})


def test_example_prompt_hash_is_deterministic_hex():
    digest = hash_request({"prompt": "Hello", "model": "test"})
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    assert digest == hash_request({"model": "test", "prompt": "Hello"})


def test_int_and_bool_differ():
    assert hash_request(1) != hash_request(True)


def test_list_order_matters_and_tuple_differs_from_list():
    assert hash_request([1, 2]) != hash_request([2, 1])
    assert hash_request((1, 2)) != hash_request([1, 2])


def test_unsupported_object_raises_type_error():
    class Custom:
        pass

    with pytest.raises(TypeError) as exc_info:
        hash_request(Custom())
    assert "Custom" in str(exc_info.value)


def test_non_string_dict_key_raises_type_error():
    with pytest.raises(TypeError):
        hash_request({1: "x"})
