"""Deterministically preserve paid-membership identity facts.

Membership events are runtime data supplied by the caller; this module contains no
viewer records. It converts eligible events into protected identity facts without
depending on an LLM extractor.
"""

from datetime import datetime

from memory.models import FACT_ACTIVE


GUARD_NAMES = ("总督", "提督", "舰长")
GUARD_FACT_CATEGORY = "identity"
GUARD_FACT_KEY = "大航海"


def _parse_guard_name(text: str) -> str:
    marker = "开通了"
    index = (text or "").rfind(marker)
    if index == -1:
        return "大航海"
    name = text[index + len(marker):].strip()
    return name or "大航海"


def backfill_guard_facts(storage, session_id=None, log_fn=None) -> int:
    """Backfill missing membership facts for one session or the whole store."""
    log = log_fn or (lambda message: None)
    rows = storage.list_guard_messages(session_id)
    written = 0
    for row in rows:
        identity_key = row.get("identity_key")
        display_name = row.get("viewer_name")
        if not identity_key:
            continue
        if storage.has_active_viewer_fact(
            identity_key, GUARD_FACT_CATEGORY, GUARD_FACT_KEY
        ):
            continue
        guard_name = _parse_guard_name(row.get("message_text", ""))
        created_at = row.get("created_at") or _now_iso()
        date = created_at[:10]
        fact_value = (
            f"已开通{guard_name}（大航海，{date}）"
            if date
            else f"已开通{guard_name}（大航海）"
        )
        storage.add_viewer_fact(
            identity_key=identity_key,
            display_name=display_name,
            category=GUARD_FACT_CATEGORY,
            fact_key=GUARD_FACT_KEY,
            fact_value=fact_value,
            confidence=1.0,
            source_message_id=row.get("id"),
            status=FACT_ACTIVE,
            created_at=created_at,
            source="manual",
        )
        written += 1
        log(f"[memory.membership] {display_name} -> {fact_value}")
    if written:
        log(f"[memory.membership] backfilled {written} fact(s)")
    return written


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")
