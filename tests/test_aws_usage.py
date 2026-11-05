import json
from importlib import resources

import pytest

from ai_cost_tracker.normalise import canonical_model as cloud_canonical_model
from ai_cost_tracker.normalise import parse_meter
from ai_cost_tracker.normalise.aws_usage import (
    _compile,
    _parse,
    canonical_model,
    is_token_priced,
    known_model,
    parse_usage_type,
    strip_region,
)
from ai_cost_tracker.normalise.azure_meters import MeterInfo


def M(model, direction, dtype="global", modality=None) -> MeterInfo:
    return MeterInfo(
        model=model, version=None, direction=direction, deployment_type=dtype, modality=modality
    )


# The six usage types the fake AWS connector bills, keyed by its deployment names' models.
DEMO = {
    "Claude3.7Sonnet-input-tokens": ("claude-3-7-sonnet", "input"),
    "Claude3.7Sonnet-output-tokens": ("claude-3-7-sonnet", "output"),
    "Claude3.5Haiku-input-tokens": ("claude-3-5-haiku", "input"),
    "Claude3.5Haiku-output-tokens": ("claude-3-5-haiku", "output"),
    "NovaPro-input-tokens": ("nova-pro", "input"),
    "NovaPro-output-tokens": ("nova-pro", "output"),
}
DEMO_DEPLOYMENTS = {
    "us.anthropic.claude-3-7-sonnet-20250219-v1:0": "claude-3-7-sonnet",
    "anthropic.claude-3-5-haiku-20241022-v1:0": "claude-3-5-haiku",
    "amazon.nova-pro-v1:0": "nova-pro",
}


@pytest.mark.parametrize(("usage_type", "expected"), DEMO.items())
@pytest.mark.parametrize("prefix", ["", "USE1-"])
def test_demo_usage_types(usage_type: str, expected: tuple[str, str], prefix: str) -> None:
    info = parse_meter("aws", prefix + usage_type)
    assert info == M(*expected)


def test_demo_models_match_deployment_names() -> None:
    for name, model in DEMO_DEPLOYMENTS.items():
        assert cloud_canonical_model("aws", name) == model
        assert model in {m for m, _ in DEMO.values()}


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("anthropic.claude-3-haiku-20240307-v1:0", "claude-3-haiku"),
        ("anthropic.claude-3-haiku-20240307-v1:0:200k", "claude-3-haiku"),
        ("anthropic.claude-3-opus-20240229-v1:0", "claude-3-opus"),
        ("anthropic.claude-3-5-sonnet-20240620-v1:0", "claude-3-5-sonnet"),
        ("anthropic.claude-3-5-sonnet-20241022-v2:0", "claude-3-5-sonnet-v2"),
        ("eu.anthropic.claude-3-7-sonnet-20250219-v1:0", "claude-3-7-sonnet"),
        ("apac.anthropic.claude-sonnet-4-20250514-v1:0", "claude-sonnet-4"),
        ("us.anthropic.claude-opus-4-1-20250805-v1:0", "claude-opus-4-1"),
        ("global.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5"),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
        ("us-gov.anthropic.claude-3-5-sonnet-20240620-v1:0", "claude-3-5-sonnet"),
        ("amazon.nova-micro-v1:0", "nova-micro"),
        ("us.amazon.nova-lite-v1:0", "nova-lite"),
        ("us.amazon.nova-premier-v1:0", "nova-premier"),
        ("amazon.titan-text-express-v1", "titan-text-express"),
        ("amazon.titan-embed-text-v1", "titan-embed-text"),
        ("amazon.titan-embed-text-v2:0", "titan-embed-text-v2"),
        ("meta.llama3-8b-instruct-v1:0", "llama3-8b-instruct"),
        ("us.meta.llama3-2-11b-instruct-v1:0", "llama3-2-11b-instruct"),
        ("meta.llama3-1-405b-instruct-v1:0", "llama3-1-405b-instruct"),
        ("us.meta.llama4-scout-17b-instruct-v1:0", "llama4-scout-17b-instruct"),
        ("mistral.mistral-7b-instruct-v0:2", "mistral-7b-instruct"),
        ("mistral.mixtral-8x7b-instruct-v0:1", "mixtral-8x7b-instruct"),
        ("mistral.mistral-large-2407-v1:0", "mistral-large-2407"),
        ("cohere.command-r-plus-v1:0", "command-r-plus"),
        ("cohere.embed-english-v3", "embed-english-v3"),
        ("cohere.rerank-v3-5:0", "rerank-v3-5"),
        ("us.deepseek.r1-v1:0", "deepseek-r1"),
        ("ai21.jamba-1-5-large-v1:0", "jamba-1-5-large"),
        ("openai.gpt-oss-120b-1:0", "gpt-oss-120b"),
        ("writer.palmyra-x5-v1:0", "palmyra-x5"),
        # ARNs that end in a model id: foundation models and system inference profiles.
        (
            "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0",
            "claude-3-5-haiku",
        ),
        (
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/"
            "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
            "claude-3-7-sonnet",
        ),
        # Usage-type spellings work too.
        ("Claude3.7Sonnet", "claude-3-7-sonnet"),
        ("NovaPro", "nova-pro"),
        # Unknown models keep a readable fallback name.
        ("amazon.new-model-20260101-v1:0", "new-model"),
    ],
)
def test_canonical_model(name: str, model: str) -> None:
    assert canonical_model(name) == model


def test_opaque_profile_ids_have_no_known_model() -> None:
    arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2c3d4e5f6"
    assert known_model(arn) is None
    assert canonical_model(arn) == "a1b2c3d4e5f6"
    assert known_model("us.anthropic.claude-3-7-sonnet-20250219-v1:0") == "claude-3-7-sonnet"


@pytest.mark.parametrize(
    ("usage_type", "expected"),
    [
        ("USW2-Llama3.1-70B-input-tokens", M("llama3-1-70b-instruct", "input")),
        ("EU-Llama3-2-11B-Instruct-output-tokens", M("llama3-2-11b-instruct", "output")),
        ("EUC1-Claude3.7Sonnet-cache-read-input-tokens", M("claude-3-7-sonnet", "cached_input")),
        ("USE1-Claude3.7Sonnet-cache-write-input-tokens", M("claude-3-7-sonnet", "cache_write")),
        ("USE1-NovaLite-input-tokens-batch", M("nova-lite", "input", "batch")),
        ("USE1-NovaMicro-batch-output-tokens", M("nova-micro", "output", "batch")),
        (
            "USE1-Claude3.5Haiku-input-tokens-latency-optimized",
            M("claude-3-5-haiku", "input", "latency_optimized"),
        ),
        (
            "USE1-Claude4.5Sonnet-input-tokens-cross-region-global",
            M("claude-sonnet-4-5", "input"),
        ),
        ("USE1-ClaudeSonnet4.5-output-tokens", M("claude-sonnet-4-5", "output")),
        ("USE1-ClaudeSonnet4-input-tokens-long-context", M("claude-sonnet-4", "input_long")),
        ("USE1-Claude3.5SonnetV2-input-tokens", M("claude-3-5-sonnet-v2", "input")),
        ("USE1-Claude3.5Sonnet-input-tokens", M("claude-3-5-sonnet", "input")),
        (
            "USE1-ProvisionedThroughput-Claude3Haiku-ModelUnits",
            M("claude-3-haiku", "provisioned", "provisioned"),
        ),
        (
            "USE1-Claude3.7Sonnet-ProvisionedThroughput-1Month",
            M("claude-3-7-sonnet", "provisioned", "provisioned"),
        ),
        ("USE1-AI21-Jamba1.5Large-input-tokens", M("jamba-1-5-large", "input")),
        ("AmazonNovaPremier-output-tokens", M("nova-premier", "output")),
        ("USE1-TitanEmbeddingsV2-Text-input-tokens", M("titan-embed-text-v2", "input")),
        ("USE1-TitanEmbeddingsG1-Text", M("titan-embed-text", "input")),
        (
            "USE1-TitanImageGeneratorG1-1024-standard-images",
            M("titan-image-generator", "output", modality="image"),
        ),
        ("USE1-NovaCanvas-T2I-1024", M("nova-canvas", "output", modality="image")),
        ("USE1-NovaReel-video-seconds", M("nova-reel", "output", modality="video")),
        ("USE1-NovaSonic-audio-input-tokens", M("nova-sonic", "input", modality="audio")),
        ("USE1-DeepSeek-R1-input-tokens", M("deepseek-r1", "input")),
        ("USE1-Mistral-Large-2407-output-tokens", M("mistral-large-2407", "output")),
        ("USE1-CohereRerank3.5-search-units", M("rerank-v3-5", "other")),
        ("USE1-gpt-oss-120b-output-tokens", M("gpt-oss-120b", "output")),
        # No known model -> unmapped queue.
        ("USE1-MP:USE1_InputTokenCount-Units", None),
        ("USE1-UnknownModel-input-tokens", None),
        ("USE1-CustomModelStorage-ModelMonth", None),
        ("", None),
    ],
)
def test_parse_usage_type(usage_type: str, expected: MeterInfo | None) -> None:
    assert parse_usage_type(usage_type) == expected


def test_usage_types_and_model_ids_meet() -> None:
    pairs = {
        "USE1-Claude3Haiku-input-tokens": "anthropic.claude-3-haiku-20240307-v1:0",
        "USE1-Claude4Sonnet-input-tokens": "us.anthropic.claude-sonnet-4-20250514-v1:0",
        "USE1-Claude4.1Opus-input-tokens": "us.anthropic.claude-opus-4-1-20250805-v1:0",
        "USE1-Claude4.5Haiku-input-tokens": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "USE1-NovaMicro-input-tokens": "amazon.nova-micro-v1:0",
        "USE1-Llama3.3-70B-input-tokens": "us.meta.llama3-3-70b-instruct-v1:0",
        "USE1-Llama4Maverick17B-input-tokens": "us.meta.llama4-maverick-17b-instruct-v1:0",
        "USE1-Mixtral8x7B-input-tokens": "mistral.mixtral-8x7b-instruct-v0:1",
        "USE1-CommandRPlus-input-tokens": "cohere.command-r-plus-v1:0",
        "USE1-DeepSeekR1-input-tokens": "us.deepseek.r1-v1:0",
        "USE1-Jamba1.5Mini-input-tokens": "ai21.jamba-1-5-mini-v1:0",
        "USE1-TitanTextExpress-input-tokens": "amazon.titan-text-express-v1",
    }
    for usage_type, model_id in pairs.items():
        info = parse_usage_type(usage_type)
        assert info is not None, usage_type
        assert info.model == canonical_model(model_id), usage_type


def test_strip_region() -> None:
    assert strip_region("USE1-Claude3.7Sonnet-input-tokens") == "Claude3.7Sonnet-input-tokens"
    assert strip_region("EU-NovaPro-input-tokens") == "NovaPro-input-tokens"
    assert strip_region("NovaPro-input-tokens") == "NovaPro-input-tokens"
    assert strip_region("AI21-Jamba1.5Large-input-tokens") == "AI21-Jamba1.5Large-input-tokens"


def test_is_token_priced() -> None:
    assert is_token_priced("USE1-NovaPro-input-tokens")
    assert is_token_priced("USE1-TitanEmbeddingsV2-Text-input-tokens")
    assert not is_token_priced("USE1-TitanImageGeneratorG1-1024-standard-images")
    assert not is_token_priced("USE1-CohereRerank3.5-search-units")
    assert not is_token_priced("USE1-UnknownModel-input-tokens")


def _rules_data() -> dict:
    text = resources.files("ai_cost_tracker.normalise").joinpath("aws_usage_rules.json")
    return json.loads(text.read_text("utf-8"))


def test_rules_file_is_data_driven() -> None:
    data = _rules_data()
    assert "Assumption (spike-aws)" in data["_unverified"]
    data["models"].append({"name": "grok-4", "aliases": ["Grok4Fast"]})
    rules = _compile(data)
    parsed = _parse("USE1-Grok4Fast-input-tokens", rules)
    assert parsed is not None and parsed.info.model == "grok-4"
    assert canonical_model("grok-4-v1:0", rules) == "grok-4"
    assert parse_usage_type("USE1-Grok4Fast-input-tokens") is None


def test_rules_reject_ambiguous_aliases() -> None:
    data = _rules_data()
    data["models"].append({"name": "other-model", "aliases": ["Claude3.7Sonnet"]})
    with pytest.raises(ValueError, match="two models"):
        _compile(data)
