"""Parse Bedrock usage types into model, direction and deployment type.

Like ``gcp_skus``, the vocabulary lives in a rules file (``aws_usage_rules.json``) so a new
usage-type spelling is a data change. ``canonical_model`` is the one name shared by CUR usage
types, CloudWatch ``ModelId`` values (including cross-region ``us.``/``eu.`` profiles and ARNs
that end in a model id) and deployments, so billed cost, tokens and prices join.

Only the demo usage types are known spellings; the rest are inferred from AWS's public
Bedrock pricing and model-id docs (Assumption (spike-aws)). Usage types that don't parse are
queued for an admin, not dropped.
"""

import json
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

from ai_cost_tracker.normalise.azure_meters import MeterInfo

_SPLIT = re.compile(r"[^a-z0-9.]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Longest run of tokens tried as a model name; real names are at most a handful of tokens.
_MAX_MODEL_TOKENS = 8
_RuleList = tuple[tuple[frozenset[str], str], ...]


@dataclass(frozen=True)
class _FamilyDefault:
    direction: str | None
    modality: str | None
    tokens: bool


@dataclass(frozen=True)
class _Rules:
    region_prefix: re.Pattern[str]
    geo_prefix: re.Pattern[str]
    provider_prefix: re.Pattern[str]
    compact_providers: tuple[str, ...]
    model_id_rewrites: tuple[tuple[re.Pattern[str], str], ...]
    optional_suffixes: tuple[str, ...]
    aliases: dict[str, str]  # compact spelling -> canonical name
    family_of: dict[str, _FamilyDefault]  # canonical name -> default when no direction
    phrase_rewrites: tuple[tuple[re.Pattern[str], str], ...]
    skip_leading: frozenset[str]
    direction: _RuleList
    deployment: _RuleList
    default_deployment: str
    modality: _RuleList


def compact(text: str) -> str:
    """Letters and digits only: ``Claude3.7Sonnet`` and ``claude-3-7-sonnet`` meet here."""
    return _NON_ALNUM.sub("", text.lower())


def _rule_list(rows: list[dict[str, Any]], key: str) -> _RuleList:
    return tuple((frozenset(r["all"]), r[key]) for r in rows)


def _alternation(words: list[str]) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


def _compile(data: dict[str, Any]) -> _Rules:
    direction = _rule_list(data["direction_rules"], "direction")
    deployment = _rule_list(data["deployment_rules"], "deployment_type")
    modality = _rule_list(data.get("modality_rules", []), "modality")
    rule_tokens = {t for toks, _ in direction + deployment + modality for t in toks}
    suffixes = tuple(compact(s) for s in data.get("optional_suffixes", []))
    defaults = {
        k: _FamilyDefault(v.get("direction"), v.get("modality"), bool(v.get("tokens", True)))
        for k, v in data.get("family_defaults", {}).items()
    }
    aliases: dict[str, str] = {}
    family_of: dict[str, _FamilyDefault] = {}
    for model in data["models"]:
        name = model["name"]
        for spelling in (name, *model.get("aliases", [])):
            key = compact(spelling)
            keys = {key} | {key[: -len(s)] for s in suffixes if key.endswith(s) and key != s}
            for k in keys:
                if aliases.setdefault(k, name) != name:
                    raise ValueError(f"aws usage rules: {spelling!r} maps to two models")
        if fam := model.get("family_default"):
            family_of[name] = defaults[fam]
    return _Rules(
        region_prefix=re.compile(data["region_prefix"]),
        geo_prefix=re.compile(rf"^(?:{_alternation(data['geo_prefixes'])})\."),
        provider_prefix=re.compile(rf"^({_alternation(data['providers'])})\."),
        compact_providers=tuple(data.get("compact_provider_prefixes", [])),
        model_id_rewrites=tuple(
            (re.compile(r["pattern"]), r["replace"]) for r in data.get("model_id_rewrites", [])
        ),
        optional_suffixes=suffixes,
        aliases=aliases,
        family_of=family_of,
        phrase_rewrites=tuple(
            (re.compile(r["pattern"]), r["replace"]) for r in data.get("phrase_rewrites", [])
        ),
        skip_leading=frozenset(rule_tokens | set(data.get("skip_leading_tokens", []))),
        direction=direction,
        deployment=deployment,
        default_deployment=data.get("default_deployment_type", "global"),
        modality=modality,
    )


@cache
def _rules() -> _Rules:
    text = resources.files(__package__).joinpath("aws_usage_rules.json").read_text("utf-8")
    return _compile(json.loads(text))


def _lookup(key: str, rules: _Rules) -> str | None:
    """Canonical name for a compact spelling, also without a glued provider prefix
    (``amazonnovapro``) or a trailing optional word."""
    candidates = [key]
    candidates += [key[len(p) :] for p in rules.compact_providers if key.startswith(p)]
    for cand in list(candidates):
        candidates += [cand[: -len(s)] for s in rules.optional_suffixes if cand.endswith(s)]
    return next((rules.aliases[c] for c in candidates if c and c in rules.aliases), None)


def _strip_model_id(name: str, rules: _Rules) -> tuple[str, str | None]:
    """(model id without geo prefix, date and version; its provider or None)."""
    text = name.strip().lower().rsplit("/", 1)[-1]
    text = rules.geo_prefix.sub("", text)
    provider = None
    if m := rules.provider_prefix.match(text):
        provider, text = m.group(1), text[m.end() :]
    for pattern, replace in rules.model_id_rewrites:
        text = pattern.sub(replace, text)
    return text.strip("-"), provider


def known_model(name: str, rules: _Rules | None = None) -> str | None:
    """Canonical name when ``name`` (ModelId, ARN or usage-type model spelling) is a known
    model; None for opaque ids such as application inference profile or provisioned model
    ARNs, whose model must come from the Bedrock API instead."""
    rules = rules or _rules()
    text, provider = _strip_model_id(name, rules)
    if provider and (hit := _lookup(compact(f"{provider}{text}"), rules)):
        return hit  # "deepseek.r1" -> deepseek-r1
    return _lookup(compact(text), rules) if text else None


def canonical_model(name: str, rules: _Rules | None = None) -> str:
    """``us.anthropic.claude-3-7-sonnet-20250219-v1:0``, its inference-profile ARN and the
    usage-type spelling ``Claude3.7Sonnet`` all become ``claude-3-7-sonnet``. Unknown models
    fall back to the id without geo prefix, provider, date and version."""
    rules = rules or _rules()
    if hit := known_model(name, rules):
        return hit
    text, _ = _strip_model_id(name, rules)
    return text or name.strip().lower()


def _first_match(rules: _RuleList, tokens: set[str]) -> str | None:
    return next((value for need, value in rules if need <= tokens), None)


def _tokens(usage_type: str, rules: _Rules) -> list[str]:
    text = rules.region_prefix.sub("", usage_type.strip(), count=1).lower()
    for pattern, replace in rules.phrase_rewrites:
        text = pattern.sub(replace, text)
    return [t for t in (t.strip(".") for t in _SPLIT.split(text)) if t]


def _find_model(tokens: list[str], rules: _Rules) -> tuple[str, int, int] | None:
    """(canonical, start, end) of the longest known model run, after leading qualifiers."""
    for start in range(len(tokens)):
        if start and tokens[start - 1] not in rules.skip_leading:
            return None
        stop = min(len(tokens), start + _MAX_MODEL_TOKENS)
        for end in range(stop, start, -1):
            if hit := _lookup(compact("".join(tokens[start:end])), rules):
                return hit, start, end
    return None


@dataclass(frozen=True)
class _Parsed:
    info: MeterInfo
    tokens: bool


def _parse(usage_type: str, rules: _Rules) -> _Parsed | None:
    tokens = _tokens(usage_type, rules)
    found = _find_model(tokens, rules)
    if found is None:
        return None
    model, start, end = found
    rest = set(tokens[:start] + tokens[end:])
    default = rules.family_of.get(model)
    direction = _first_match(rules.direction, rest)
    modality = _first_match(rules.modality, set(tokens[end:]))
    info = MeterInfo(
        model=model,
        version=None,
        direction=direction or (default.direction if default else None) or "other",
        deployment_type=_first_match(rules.deployment, rest) or rules.default_deployment,
        modality=modality or (default.modality if default else None),
    )
    return _Parsed(info=info, tokens=default.tokens if default else True)


def parse_usage_type(usage_type: str) -> MeterInfo | None:
    """Parsed usage type (with or without its region prefix), or None when no known model is
    named (-> unmapped queue)."""
    parsed = _parse(usage_type, _rules())
    return parsed.info if parsed else None


def is_token_priced(usage_type: str) -> bool:
    """False for image / video generators and rerank, billed per image, second or search unit,
    so the price sync never stores ``tokens_per_unit`` for them."""
    parsed = _parse(usage_type, _rules())
    return bool(parsed and parsed.tokens)


def strip_region(usage_type: str) -> str:
    """``USE1-Claude3.7Sonnet-input-tokens`` -> ``Claude3.7Sonnet-input-tokens`` (the
    ``sku_meter`` form shared by CUR cost lines and prices)."""
    return _rules().region_prefix.sub("", usage_type.strip(), count=1)
