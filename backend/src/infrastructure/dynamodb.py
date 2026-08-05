"""Accessor for the single DynamoDB table over ``src/infrastructure/aws.py``'s
``resource()`` factory, so ``AWS_ENDPOINT_URL``/LocalStack keeps working for
every context that needs the table (``library/`` from phase 2; ``reading``/
``chat`` in phases 5-7 reuse this instead of each rolling their own).

Must be called at request time, not import time, so tests (moto's
``mock_aws()``) and LocalStack both work — see
``tests/contexts/library/conftest.py``.
"""

from __future__ import annotations

from typing import Any

from src.config import settings
from src.infrastructure.aws import resource


def library_table() -> Any:
    """Return the boto3 Table resource for ``settings.table_name`` — the
    single table described in ``IMPLEMENTATION_PLAN.md`` (PK/SK schema
    matching ``infra/stacks/storage_stack.py`` and ``local/setup.sh``)."""
    return resource("dynamodb").Table(settings.table_name)
