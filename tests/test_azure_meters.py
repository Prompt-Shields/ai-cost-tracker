from datetime import date

import pytest

from ai_cost_tracker.connectors.fake import FakeConnector
from ai_cost_tracker.normalise.azure_meters import MeterInfo, _compile, _rules, parse_meter

M = MeterInfo

CASES = [
    # (meter, expected)
    ("gpt-4o-0806 Inp glbl", M("gpt-4o", "0806", "input", "global")),
    ("gpt-4o-0806 Outp glbl", M("gpt-4o", "0806", "output", "global")),
    ("gpt-4o-mini-0718-Outp-regnl", M("gpt-4o-mini", "0718", "output", "regional")),
    ("gpt 4o 0513 Input regional Tokens", M("gpt-4o", "0513", "input", "regional")),
    ("gpt-4.1-mini cchd Inp DZone", M("gpt-4.1-mini", None, "cached_input", "data_zone")),
    ("o3-mini-0131 Batch Inp glbl", M("o3-mini", "0131", "input", "batch")),
    ("GPT-4o Provisioned Managed", M("gpt-4o", None, "provisioned", "provisioned")),
    ("gpt-4o-mini-0718 FTuned Training", M("gpt-4o-mini", "0718", "training", None)),
    ("gpt-4o-mini-0718 FTuned Hosting", M("gpt-4o-mini", "0718", "hosting", None)),
    ("text-embedding-3-small Tokens", M("text-embedding-3-small", None, "other", None)),
    ("Dall-E 3 Standard 1024 Images", M("dall-e-3", None, "other", None, "image")),
    ("gpt-4o-0806 Inp regnl 1M Tokens", M("gpt-4o", "0806", "input", "regional")),
    ("GPT-4o-mini Inp glbl", M("gpt-4o-mini", None, "input", "global")),
    ("gpt-4o-2024-08-06 Outp glbl", M("gpt-4o", "0806", "output", "global")),
    ("gpt-4.1 Inp Data Zone", M("gpt-4.1", None, "input", "data_zone")),
    ("gpt-4.1-nano-0414 cached Inp glbl", M("gpt-4.1-nano", "0414", "cached_input", "global")),
    ("o1 Outp glbl", M("o1", None, "output", "global")),
    ("o4-mini-0416 Inp DZone", M("o4-mini", "0416", "input", "data_zone")),
    ("GPT_4O_MINI_0718_INP_GLBL", M("gpt-4o-mini", "0718", "input", "global")),
    ("  gpt-4o   0806   inp   glbl  ", M("gpt-4o", "0806", "input", "global")),
    ("gpt-35-turbo-0125 Inp regnl", M("gpt-35-turbo", "0125", "input", "regional")),
    ("gpt-3.5-turbo Outp regnl", M("gpt-35-turbo", None, "output", "regional")),
    (
        "gpt-4o-realtime-preview-1217 Aud Inp glbl",
        M("gpt-4o-realtime-preview", "1217", "input", "global", "audio"),
    ),
    ("gpt-4o PTU", M("gpt-4o", None, "provisioned", "provisioned")),
    ("gpt-4o-0806 glbl", M("gpt-4o", "0806", "other", "global")),
    ("whisper Hours", M("whisper", None, "other", None)),
    ("gpt-5-mini Inp glbl", M("gpt-5-mini", None, "input", "global")),
    # M1 spellings inferred from price lists; unverified until issue #2 sees real invoices.
    ("gpt-4o-0806 cchd Inp DZ", M("gpt-4o", "0806", "cached_input", "data_zone")),
    ("gpt 4o 0806 Cached Input Global Tokens", M("gpt-4o", "0806", "cached_input", "global")),
    ("gpt-4o-mini-0718 Batch Outp glbl", M("gpt-4o-mini", "0718", "output", "batch")),
    ("gpt-5-nano Outp DZ", M("gpt-5-nano", None, "output", "data_zone")),
    ("gpt-5 Inp dzone", M("gpt-5", None, "input", "data_zone")),
    ("gpt-4.1-mini-0414 Outp DZone", M("gpt-4.1-mini", "0414", "output", "data_zone")),
    ("gpt-4.1-nano Batch Inp glbl", M("gpt-4.1-nano", None, "input", "batch")),
    ("gpt-4o Provisioned Managed Global", M("gpt-4o", None, "provisioned", "provisioned")),
    (
        "gpt-4o-realtime-preview Txt Inp glbl",
        M("gpt-4o-realtime-preview", None, "input", "global", "text"),
    ),
    (
        "gpt-4o-mini-realtime-preview-1217 Audio Input Global",
        M("gpt-4o-mini-realtime-preview", "1217", "input", "global", "audio"),
    ),
    (
        "gpt-4o-audio-preview Aud Outp glbl",
        M("gpt-4o-audio-preview", None, "output", "global", "audio"),
    ),
    ("o1 1217 cchd Inp glbl", M("o1", "1217", "cached_input", "global")),
    ("o1-mini-0912 Outp regnl", M("o1-mini", "0912", "output", "regional")),
    ("o4-mini Batch Inp glbl", M("o4-mini", None, "input", "batch")),
    ("gpt-5-mini cchd Inp glbl", M("gpt-5-mini", None, "cached_input", "global")),
    ("text-embedding-ada-002 Tokens", M("text-embedding-ada-002", None, "other", None)),
    ("text-embedding-3-large Batch Tokens", M("text-embedding-3-large", None, "other", "batch")),
    ("Ada Embeddings", M("text-embedding-ada-002", None, "other", None)),
    ("Dall-E-3 HD 1792 Images", M("dall-e-3", None, "other", None, "image")),
    ("gpt-image-1 Inp glbl", M("gpt-image-1", None, "input", "global")),
    ("Whisper Speech to Text Batch", M("whisper", None, "other", "batch")),
    ("tts-hd 1M Characters", M("tts-hd", None, "other", None)),
    # Not AI model meters, or unknown families.
    ("Standard LRS Data Stored", None),
    ("P10 Disks", None),
    ("Inp glbl", None),
    ("", None),
    ("llama-3-70b Inp glbl", None),
    ("1024 Images", None),
]


@pytest.mark.parametrize(("meter", "expected"), CASES, ids=[c[0] or "<empty>" for c in CASES])
def test_parse_meter(meter: str, expected: MeterInfo | None) -> None:
    assert parse_meter(meter) == expected


def test_new_family_is_a_data_change() -> None:
    data = {
        "model_families": ["^llama-"],
        "direction_rules": [{"all": ["inp"], "direction": "input"}],
        "deployment_rules": [{"all": ["glbl"], "deployment_type": "global"}],
    }
    assert parse_meter("llama-3-70b Inp glbl", rules=_compile(data)) == M(
        "llama-3-70b", None, "input", "global"
    )


def test_packaged_rules_load() -> None:
    assert _rules().families


async def test_fake_connector_meters_all_parse() -> None:
    fake = FakeConnector()
    [account] = await fake.list_accounts()
    records = await fake.pull_cost(account.account_id, date(2026, 1, 5), date(2026, 1, 5))
    assert records
    for rec in records:
        info = parse_meter(rec.sku_meter)
        assert info is not None, rec.sku_meter
        assert info.model in {"gpt-4o", "gpt-4o-mini", "o3-mini"}
        assert info.version is not None and len(info.version) == 4
        assert info.direction in {"input", "output"}
        assert info.deployment_type == "global"
