from pathlib import Path


def test_streamlit_dashboard_defaults_to_loopback_only():
    config = Path(".streamlit/config.toml").read_text(encoding="utf-8")
    assert "[server]" in config
    assert 'address = "127.0.0.1"' in config


def test_generated_secrets_and_financial_exports_are_ignored():
    rules = Path(".gitignore").read_text(encoding="utf-8")
    for pattern in (
        ".env",
        ".streamlit/secrets.toml",
        "data/*.sqlite3",
        "reports/*.csv",
        "reports/*.html",
        "reports/*.json",
    ):
        assert pattern in rules
