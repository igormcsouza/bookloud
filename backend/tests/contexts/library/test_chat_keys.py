import pytest
from datetime import datetime, timezone
from src.contexts.library.infrastructure.keys import (
    sk_chat, sk_chat_quota, CHAT_PREFIX, CHAT_QUOTA_PREFIX
)

def test_sk_chat_monotonicity():
    # Test monotonicity including zero-microsecond case
    # Format required: YYYY-MM-DDTHH:MM:SS.ffffff+00:00 (fixed 32 chars)
    t1 = "2026-08-12T14:15:22.000000+00:00"
    t2 = "2026-08-12T14:15:22.000001+00:00"
    
    sk1 = sk_chat(t1, "msg1")
    sk2 = sk_chat(t2, "msg2")
    
    assert sk1 < sk2
    assert len(sk1) == len(CHAT_PREFIX) + 32 + 1 + 4
    
def test_sk_chat_quota():
    assert sk_chat_quota("2026-08-12") == f"{CHAT_QUOTA_PREFIX}2026-08-12"
