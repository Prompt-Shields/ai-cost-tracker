"""Parse Azure OpenAI / Foundry meter names into model, version, direction and deployment type.

Azure's meter names are inconsistent (`gpt-4o-0806 Inp glbl`, `gpt 4o 0513 Input regional
Tokens`, ...), so the vocabulary lives in `azure_meter_rules.json`: a new meter spelling should
be a data change, not a code change (ARCHITECTURE.md §4.2).
"""

import json
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

_SPLIT = re.compile(r"[\s\-_]+")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_MMDD = re.compile(r"^(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")


@dataclass(frozen=True)
class MeterInfo:
    model: str  # e.g. "gpt-4o", "gpt-4o-mini", "o3-mini"
    version: str | None  # MMDD, e.g. "0806"
    direction: str  # input|cached_input|output|training|hosting|provisioned|other
    deployment_type: str | None  # global|data_zone|regional|batch|provisioned


@dataclass(frozen=True)
class _Rules:
    families: tuple[re.Pattern[str], ...]
    aliases: dict[str, str]
    direction: tuple[tuple[frozenset[str], str], ...]
    deployment: tuple[tuple[frozenset[str], str], ...]
    stop: frozenset[str]


def _compile(data: dict[str, Any]) -> _Rules:
    direction = tuple((frozenset(r["all"]), r["direction"]) for r in data["direction_rules"])
    deployment = tuple(
        (frozenset(r["all"]), r["deployment_type"]) for r in data["deployment_rules"]
    )
    rule_tokens = {t for toks, _ in direction + deployment for t in toks}
    return _Rules(
        families=tuple(re.compile(p) for p in data["model_families"]),
        aliases=dict(data.get("model_aliases", {})),
        direction=direction,
        deployment=deployment,
        stop=frozenset(rule_tokens | set(data.get("stop_tokens", []))),
    )


@cache
def _rules() -> _Rules:
    text = resources.files(__package__).joinpath("azure_meter_rules.json").read_text("utf-8")
    return _compile(json.loads(text))


def _first_match(rules: tuple[tuple[frozenset[str], str], ...], tokens: set[str]) -> str | None:
    return next((value for need, value in rules if need <= tokens), None)


def parse_meter(meter: str, rules: _Rules | None = None) -> MeterInfo | None:
    """Return the parsed meter, or None when no known model family is recognised."""
    rules = rules or _rules()
    # YYYY-MM-DD would be split apart by the tokeniser, so fold it to MMDD first.
    text = _ISO_DATE.sub(lambda m: f"{m[2]}{m[3]}", meter.strip().lower())
    tokens = [t for t in _SPLIT.split(text) if t]

    model_tokens: list[str] = []
    version: str | None = None
    for tok in tokens:
        if _MMDD.match(tok) and model_tokens:
            version = tok
            break
        if tok in rules.stop:
            break
        model_tokens.append(tok)
    model = "-".join(model_tokens)
    if not model or not any(f.search(model) for f in rules.families):
        return None
    model = rules.aliases.get(model, model)

    token_set = set(tokens)
    return MeterInfo(
        model=model,
        version=version,
        direction=_first_match(rules.direction, token_set) or "other",
        deployment_type=_first_match(rules.deployment, token_set),
    )
