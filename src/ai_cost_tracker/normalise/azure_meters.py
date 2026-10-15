"""Parse Azure OpenAI / Foundry meter names into model, version, direction and deployment type.

Azure's meter names are inconsistent (`gpt-4o-0806 Inp glbl`, `gpt 4o 0513 Input regional
Tokens`, ...), so the vocabulary lives in `azure_meter_rules.json`: a new meter spelling should
be a data change, not a code change (ARCHITECTURE.md §4.2).

Most spellings in the rules file are guesses from public price lists, not from real invoices;
they stay unverified until the live spike (issue #2) collects actual meter names. Meters that
don't parse are not dropped: ingestion queues them for an admin (`UnmappedMeter`).
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
    # audio|text|image where the meter says so (realtime audio and text tokens are priced
    # differently); None when the meter doesn't name a modality.
    modality: str | None = None


@dataclass(frozen=True)
class _Rules:
    families: tuple[re.Pattern[str], ...]
    aliases: dict[str, str]
    direction: tuple[tuple[frozenset[str], str], ...]
    deployment: tuple[tuple[frozenset[str], str], ...]
    modality: tuple[tuple[frozenset[str], str], ...]
    stop: frozenset[str]
    # Model names that contain a stop token (e.g. "gpt-4o-audio-preview"), longest first.
    known_models: tuple[tuple[str, ...], ...]


def _compile(data: dict[str, Any]) -> _Rules:
    direction = tuple((frozenset(r["all"]), r["direction"]) for r in data["direction_rules"])
    deployment = tuple(
        (frozenset(r["all"]), r["deployment_type"]) for r in data["deployment_rules"]
    )
    modality = tuple((frozenset(r["all"]), r["modality"]) for r in data.get("modality_rules", []))
    rule_tokens = {t for toks, _ in direction + deployment + modality for t in toks}
    known = {tuple(t for t in _SPLIT.split(m.lower()) if t) for m in data.get("known_models", [])}
    return _Rules(
        families=tuple(re.compile(p) for p in data["model_families"]),
        aliases=dict(data.get("model_aliases", {})),
        direction=direction,
        deployment=deployment,
        modality=modality,
        stop=frozenset(rule_tokens | set(data.get("stop_tokens", []))),
        known_models=tuple(sorted(known, key=len, reverse=True)),
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

    # A known model name may contain a stop token ("audio", "image", "hd"); take it whole.
    known = next((k for k in rules.known_models if tuple(tokens[: len(k)]) == k), ())
    model_tokens: list[str] = list(known)
    version: str | None = None
    for tok in tokens[len(known) :]:
        if _MMDD.match(tok) and model_tokens:
            version = tok
            break
        if tok in rules.stop:
            break
        model_tokens.append(tok)
    model = "-".join(model_tokens)
    model = rules.aliases.get(model, model)
    if not model or not any(f.search(model) for f in rules.families):
        return None

    token_set = set(tokens)
    return MeterInfo(
        model=model,
        version=version,
        direction=_first_match(rules.direction, token_set) or "other",
        deployment_type=_first_match(rules.deployment, token_set),
        # Only tokens after a known name count, so "gpt-image-1" alone isn't an image meter.
        modality=_first_match(rules.modality, set(tokens[len(known) :])),
    )
