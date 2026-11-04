"""Parse Vertex AI SKU descriptions into model, direction and deployment type.

Like ``azure_meters``, the vocabulary lives in a rules file (``gcp_sku_rules.json``) so a new
SKU spelling is a data change. ``canonical_model`` is the one name shared by SKUs, Cloud
Monitoring's ``model_user_id`` and deployments, so billed cost, tokens and prices join.

Only the demo SKUs are known spellings; the rest are inferred from Google's public pricing page
(Assumption (spike-gcp)). SKUs that don't parse are queued for an admin, not dropped.
"""

import json
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

from ai_cost_tracker.normalise.azure_meters import MeterInfo

_SPLIT = re.compile(r"[^a-z0-9.]+")
_VERSION_TOKEN = re.compile(r"^\d{3}$")
_VERSION_SUFFIX = re.compile(r"-(\d{3})$")
# "gemini-2.5-pro-preview-05-06" -> the date pins a preview build, like a version.
_PREVIEW_DATE = re.compile(r"(-preview)-(\d{2}-\d{2}(?:-\d{2,4})?)$")
_AT_VERSION = re.compile(r"@([\w.-]+)$")
_RuleList = tuple[tuple[frozenset[str], str], ...]


@dataclass(frozen=True)
class _FamilyDefault:
    pattern: re.Pattern[str]
    direction: str | None
    modality: str | None
    tokens: bool


@dataclass(frozen=True)
class _Rules:
    families: tuple[re.Pattern[str], ...]
    known_models: tuple[tuple[str, ...], ...]
    skip_leading: frozenset[str]
    phrase_rewrites: tuple[tuple[re.Pattern[str], str], ...]
    model_rewrites: tuple[tuple[re.Pattern[str], str], ...]
    numeric_name: tuple[re.Pattern[str], ...]
    direction: _RuleList
    deployment: _RuleList
    default_deployment: str
    modality: _RuleList
    family_defaults: tuple[_FamilyDefault, ...]
    stop: frozenset[str]


def _rule_list(rows: list[dict[str, Any]], key: str) -> _RuleList:
    return tuple((frozenset(r["all"]), r[key]) for r in rows)


def _compile(data: dict[str, Any]) -> _Rules:
    direction = _rule_list(data["direction_rules"], "direction")
    deployment = _rule_list(data["deployment_rules"], "deployment_type")
    modality = _rule_list(data.get("modality_rules", []), "modality")
    rule_tokens = {t for toks, _ in direction + deployment + modality for t in toks}
    known = {tuple(_SPLIT.split(m.lower())) for m in data.get("known_models", [])}
    return _Rules(
        families=tuple(re.compile(p) for p in data["model_families"]),
        known_models=tuple(sorted(known, key=len, reverse=True)),
        skip_leading=frozenset(data.get("skip_leading_tokens", [])),
        phrase_rewrites=tuple(
            (re.compile(r["pattern"]), r["replace"]) for r in data.get("phrase_rewrites", [])
        ),
        model_rewrites=tuple(
            (re.compile(r["pattern"]), r["replace"]) for r in data.get("model_rewrites", [])
        ),
        numeric_name=tuple(re.compile(p) for p in data.get("numeric_name_families", [])),
        direction=direction,
        deployment=deployment,
        default_deployment=data.get("default_deployment_type", "global"),
        modality=modality,
        family_defaults=tuple(
            _FamilyDefault(
                pattern=re.compile(r["pattern"]),
                direction=r.get("direction"),
                modality=r.get("modality"),
                tokens=bool(r.get("tokens", True)),
            )
            for r in data.get("family_defaults", [])
        ),
        stop=frozenset(rule_tokens | set(data.get("stop_tokens", []))),
    )


@cache
def _rules() -> _Rules:
    text = resources.files(__package__).joinpath("gcp_sku_rules.json").read_text("utf-8")
    return _compile(json.loads(text))


def _first_match(rules: _RuleList, tokens: set[str]) -> str | None:
    return next((value for need, value in rules if need <= tokens), None)


def _base_name(name: str) -> str:
    """Lowercased, hyphenated last path segment without an ``@version`` suffix."""
    text = name.strip().lower().rsplit("/", 1)[-1]
    text = _AT_VERSION.sub("", text)
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-{2,}", "-", text).strip("-")


def _numeric_is_name(model: str, rules: _Rules) -> bool:
    return any(p.search(model) for p in rules.numeric_name)


def canonical_model(name: str, rules: _Rules | None = None) -> str:
    """``gemini-2.5-flash-001``, ``Gemini 2.5 Flash`` and ``publishers/google/models/
    gemini-2.5-flash`` all become ``gemini-2.5-flash``; ``claude-3-5-sonnet-v2@20241022``
    becomes ``claude-3.5-sonnet-v2`` to match the SKU spelling."""
    rules = rules or _rules()
    text = _base_name(name)
    if not _numeric_is_name(text, rules):
        text = _VERSION_SUFFIX.sub("", text)
    text = _PREVIEW_DATE.sub(r"\1", text)
    for pattern, replace in rules.model_rewrites:
        text = pattern.sub(replace, text)
    return text


def model_version(name: str, rules: _Rules | None = None) -> str | None:
    """The ``001`` in ``gemini-2.0-flash-001`` (or ``20241022`` in ``claude-...@20241022``);
    None when the name carries no version."""
    rules = rules or _rules()
    raw = name.strip().lower().rsplit("/", 1)[-1]
    if m := _AT_VERSION.search(raw):
        return m.group(1)
    text = _base_name(name)
    if m := _PREVIEW_DATE.search(text):
        return m.group(2)
    if _numeric_is_name(text, rules):
        return None
    m = _VERSION_SUFFIX.search(text)
    return m.group(1) if m else None


def _tokens(description: str, rules: _Rules) -> list[str]:
    text = description.strip().lower()
    for pattern, replace in rules.phrase_rewrites:
        text = pattern.sub(replace, text)
    tokens = [t.strip(".") for t in _SPLIT.split(text)]
    tokens = [t for t in tokens if t]
    while tokens and tokens[0] in rules.skip_leading:
        tokens.pop(0)
    return tokens


@dataclass(frozen=True)
class _Parsed:
    info: MeterInfo
    tokens: bool


def _parse(description: str, rules: _Rules) -> _Parsed | None:
    tokens = _tokens(description, rules)
    known = next((k for k in rules.known_models if tuple(tokens[: len(k)]) == k), ())
    model_tokens: list[str] = list(known)
    version: str | None = None
    for tok in tokens[len(known) :]:
        if _VERSION_TOKEN.match(tok) and model_tokens:
            # Trailing hyphen: the family patterns expect a number to follow the name.
            if _numeric_is_name("-".join(model_tokens) + "-", rules):
                model_tokens.append(tok)
                continue
            version = tok
            break
        if tok in rules.stop:
            break
        model_tokens.append(tok)
    model = canonical_model("-".join(model_tokens), rules) if model_tokens else ""
    if not model or not any(f.search(model) for f in rules.families):
        return None

    token_set = set(tokens)
    default = next((d for d in rules.family_defaults if d.pattern.search(model)), None)
    # Only tokens after a known name count, so "text embedding" alone isn't a text modality.
    modality = _first_match(rules.modality, set(tokens[len(known) :]))
    direction = _first_match(rules.direction, token_set)
    info = MeterInfo(
        model=model,
        version=version,
        direction=direction or (default.direction if default else None) or "other",
        deployment_type=_first_match(rules.deployment, token_set) or rules.default_deployment,
        modality=modality or (default.modality if default else None),
    )
    return _Parsed(info=info, tokens=default.tokens if default else True)


def parse_sku(description: str) -> MeterInfo | None:
    """Parsed SKU, or None when no known model family is recognised (-> unmapped queue)."""
    parsed = _parse(description, _rules())
    return parsed.info if parsed else None


def is_token_priced(description: str) -> bool:
    """False for SKUs billed per image / second of video (Imagen, Veo) even when their usage
    unit is a plain count, so the catalog never stores ``tokens_per_unit`` for them."""
    parsed = _parse(description, _rules())
    return bool(parsed and parsed.tokens)
