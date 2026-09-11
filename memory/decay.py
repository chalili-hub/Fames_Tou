"""Lazy time-decay filtering for durable viewer facts.

The database keeps immutable timestamps. Strength is calculated when facts are read,
so an intermittently running streamer does not need a background decay job. Hidden
facts are not deleted and may become active again when a later conversation refreshes
their timestamp.
"""

from datetime import datetime


DEFAULT_HALF_LIFE_HOURS = 168.0
DEFAULT_PRUNE_THRESHOLD = 0.1

PINNED_SOURCES = ("human", "manual")
PINNED_CATEGORIES = ("identity",)


def fact_strength(created_at, now=None, half_life_hours=DEFAULT_HALF_LIFE_HOURS):
    """Return a fact's current strength in the inclusive range 0..1."""
    if half_life_hours is None or half_life_hours <= 0:
        return 1.0
    now = now or datetime.now()
    try:
        created = datetime.fromisoformat(str(created_at))
    except (ValueError, TypeError):
        return 1.0
    age_hours = (now - created).total_seconds() / 3600.0
    if age_hours <= 0:
        return 1.0
    return 0.5 ** (age_hours / half_life_hours)


def is_pinned(fact):
    """Human corrections, deterministic facts and identity facts do not decay."""
    if (fact.get("source") or "") in PINNED_SOURCES:
        return True
    if (fact.get("category") or "") in PINNED_CATEGORIES:
        return True
    return False


def filter_active_facts(facts, now=None, half_life_hours=None, prune_threshold=None):
    """Keep pinned or sufficiently strong facts while preserving input order."""
    if half_life_hours is None or prune_threshold is None:
        configured_half_life, configured_threshold = resolve_params()
        if half_life_hours is None:
            half_life_hours = configured_half_life
        if prune_threshold is None:
            prune_threshold = configured_threshold
    now = now or datetime.now()
    kept = []
    for fact in facts:
        if is_pinned(fact):
            kept.append(fact)
            continue
        if fact_strength(
            fact.get("created_at"), now=now, half_life_hours=half_life_hours
        ) >= prune_threshold:
            kept.append(fact)
    return kept


def resolve_params():
    """Read optional live-project overrides, otherwise use public defaults."""
    try:
        import config
    except Exception:
        return DEFAULT_HALF_LIFE_HOURS, DEFAULT_PRUNE_THRESHOLD
    half_life = getattr(config, "MEMORY_DECAY_HALF_LIFE_HOURS", DEFAULT_HALF_LIFE_HOURS)
    threshold = getattr(config, "MEMORY_DECAY_PRUNE_THRESHOLD", DEFAULT_PRUNE_THRESHOLD)
    return half_life, threshold
