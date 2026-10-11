import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets its own data dir / database, no scheduler and no AI key."""
    monkeypatch.setenv("SCRAPEFLOW_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRAPEFLOW_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SCRAPEFLOW_AUTH_DISABLED", "true")  # tests/test_auth.py turns sign-in on
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for key in ("PROVIDER", "BASE_URL", "API_KEY", "MODEL"):
        monkeypatch.delenv(f"SCRAPEFLOW_AI_{key}", raising=False)

    from scrapeflow import config, db

    config.get_settings.cache_clear()
    db._engine = None
    db.init_db()
    yield
    db._engine = None
    config.get_settings.cache_clear()
