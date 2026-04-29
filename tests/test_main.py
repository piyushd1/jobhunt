import pytest
from src.main import _calc_duration

def test_calc_duration_happy_path():
    started = "2023-10-27T10:00:00"
    completed = "2023-10-27T10:00:30"
    assert _calc_duration(started, completed) == "30"

def test_calc_duration_missing_started():
    assert _calc_duration("", "2023-10-27T10:00:30") == "?"
    assert _calc_duration(None, "2023-10-27T10:00:30") == "?"

def test_calc_duration_missing_completed():
    assert _calc_duration("2023-10-27T10:00:00", "") == "?"
    assert _calc_duration("2023-10-27T10:00:00", None) == "?"

def test_calc_duration_both_missing():
    assert _calc_duration("", "") == "?"
    assert _calc_duration(None, None) == "?"

def test_calc_duration_invalid_format():
    assert _calc_duration("invalid", "2023-10-27T10:00:30") == "?"
    assert _calc_duration("2023-10-27T10:00:00", "invalid") == "?"
    assert _calc_duration("invalid", "also_invalid") == "?"
