from __future__ import annotations

from datetime import UTC
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from src.infrastructure.clock import SystemClock
from src.infrastructure.dynamodb import library_table
from src.infrastructure.ids import Uuid4IdGenerator


def test_system_clock_is_timezone_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(now)


def test_system_clock_advances() -> None:
    clock = SystemClock()
    first = clock.now()
    second = clock.now()
    assert second >= first


def test_uuid4_id_generator_returns_valid_uuid() -> None:
    generated = Uuid4IdGenerator().new_id()
    assert UUID(generated).version == 4


def test_uuid4_id_generator_returns_distinct_values() -> None:
    generator = Uuid4IdGenerator()
    assert generator.new_id() != generator.new_id()


def test_library_table_uses_settings_table_name(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.infrastructure.dynamodb as dynamodb_module

    monkeypatch.setattr(dynamodb_module.settings, "table_name", "bookloud-test-table")
    fake_resource = MagicMock()
    with patch.object(dynamodb_module, "resource", return_value=fake_resource) as mock_resource:
        library_table()
        mock_resource.assert_called_once_with("dynamodb")
        fake_resource.Table.assert_called_once_with("bookloud-test-table")
