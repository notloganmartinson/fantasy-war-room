from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})


@dataclass(frozen=True)
class PlayerAlias:
    source_name: str
    canonical_name: str
    position: str
    note: str


PLAYER_ALIASES = (
    PlayerAlias("Kenneth Gainwell", "Kenny Gainwell", "RB", "verified common first name"),
    PlayerAlias("Nick Singleton", "Nicholas Singleton", "RB", "verified common first name"),
)


def strict_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = normalized.replace("’", "'").replace("‘", "'")
    normalized = normalized.replace("'", "").replace(".", "")
    normalized = normalized.replace("-", " ")
    ascii_value = normalized.encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold()).split())


def suffix_insensitive_name(value: str) -> str:
    parts = normalize_name(value).split()
    if parts and parts[-1] in SUFFIXES:
        parts.pop()
    return " ".join(parts)


def alias_targets(value: str, position: str | None) -> tuple[str, ...]:
    source = normalize_name(value)
    targets: list[str] = []
    for alias in PLAYER_ALIASES:
        if position != alias.position:
            continue
        source_alias = normalize_name(alias.source_name)
        canonical_alias = normalize_name(alias.canonical_name)
        if source == source_alias:
            targets.append(canonical_alias)
        elif source == canonical_alias:
            targets.append(source_alias)
    return tuple(targets)
