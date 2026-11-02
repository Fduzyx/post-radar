from pathlib import Path

import pytest

from post_radar.config import ApiConfig, AppConfig, RuleConfig, SourceConfig, load_config
from post_radar.security import redact_text, safe_https_url


def test_secret_input_is_rejected_without_echo(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    marker = "SYNTHETIC-DO-NOT-ECHO"
    path.write_text(f'[api]\npassword = "{marker}"\n', encoding="utf-8")
    with pytest.raises(ValueError) as failure:
        load_config(path)
    assert marker not in str(failure.value)
    assert str(failure.value) == "invalid_configuration"


@pytest.mark.parametrize(
    "url",
    [
        "http://feed.example.invalid/rss",
        "https://localhost/rss",
        "https://127.0.0.1/rss",
        "https://127.1/rss",
        "https://127.0.1/rss",
        "https://0x7f.0.0.1/rss",
        "https://192.168.1.1/rss",
        "https://" + ":".join(("user", "pass")) + "@feed.example.invalid/rss",
        "https://feed.example.invalid/?api_key=example",
        "https://feed.example.invalid/#secret",
        "https://feed.example.invalid/%0aHeader",
        "https://feed.example.invalid:8080/rss",
    ],
)
def test_unsafe_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="unsafe_url"):
        safe_https_url(url)


def test_rules_have_stable_revisions_and_reference_sources() -> None:
    rule = RuleConfig(
        id="workshops",
        source_ids=("events",),
        keywords_any=("workshop",),
        requirements="Must offer online attendance.",
        recipients=("reader@example.invalid",),
    )
    assert rule.revision == RuleConfig.model_validate(rule.model_dump()).revision
    assert rule.revision != rule.model_copy(update={"requirements": "In person only"}).revision
    with pytest.raises(ValueError):
        AppConfig(rules=(rule,))
    AppConfig(
        sources=(SourceConfig(id="events", url="https://feed.example.invalid/rss"),), rules=(rule,)
    )


def test_api_requires_explicit_model_prices_budget() -> None:
    assert not ApiConfig().enabled
    with pytest.raises(ValueError):
        ApiConfig(enabled=True)


def test_config_paths_are_relative_to_config_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text('state_dir = "local-state"\n', encoding="utf-8")
    assert load_config(path).state_dir == tmp_path / "local-state"


def test_redactor_removes_known_secret_forms() -> None:
    value = "SYNTHETIC-DO-NOT-ECHO"
    assert value not in redact_text(f"Bearer {value} password='{value}' api_key={value}")


def test_redaction_never_expands_bounded_text() -> None:
    text = "token=x " * 6000
    assert len(redact_text(text)) <= len(text)
    assert "token=x" not in redact_text(text)
