"""Parse Bedrock usage types into model, direction and deployment type.

Minimal first cut written with the M4 foundation; W5 replaces it with a rules file. The
spellings are assumptions until ``act spike-aws`` lists real usage types
(Assumption (spike-aws)).
"""

import re

from ai_cost_tracker.normalise.azure_meters import MeterInfo

_REGION_PREFIX = re.compile(r"^[A-Z]{2,4}\d?-")
_GEO_PREFIX = re.compile(r"^(us|eu|apac|global|jp|au|ca)\.")
_PROVIDER = re.compile(r"^(anthropic|amazon|meta|mistral|cohere|ai21|deepseek|writer|openai)\.")
_VERSION = re.compile(r"(-v\d+)?(:\d+)?$")


def canonical_model(name: str) -> str:
    """``us.anthropic.claude-3-5-haiku-20241022-v1:0`` -> ``claude-3-5-haiku-20241022``."""
    text = name.strip().lower().rsplit("/", 1)[-1]
    text = _GEO_PREFIX.sub("", text)
    text = _PROVIDER.sub("", text)
    return _VERSION.sub("", text)


def parse_usage_type(usage_type: str) -> MeterInfo | None:
    text = _REGION_PREFIX.sub("", usage_type.strip())
    lower = text.lower()
    if "input-tokens" in lower:
        direction, model = "input", lower.split("-input-tokens")[0]
    elif "output-tokens" in lower:
        direction, model = "output", lower.split("-output-tokens")[0]
    else:
        return None
    return MeterInfo(
        model=canonical_model(model),
        version=None,
        direction=direction,
        deployment_type="batch" if "batch" in lower else "global",
    )
