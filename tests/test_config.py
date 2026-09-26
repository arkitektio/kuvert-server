"""The configuration schema loads the shipped config.yaml, and the environment overrides it."""

from pathlib import Path

from kuvert_server.configuration import Settings

CONFIG = str(Path(__file__).resolve().parent.parent / "config.yaml")


def test_config_yaml_loads(monkeypatch):
    monkeypatch.setenv("ARKITEKT_CONFIG_FILE", CONFIG)
    settings = Settings()
    assert settings.postgres.db_name == "kuvert"
    assert settings.secrets.key_path == "/secrets/kuvert.fernet"
    assert settings.sync.scheduled_every_seconds == 300
    assert settings.mail.allow_insecure is False and settings.mail.allowed_private_hosts == []
    assert settings.oauth.google is None and settings.datalayer is None


def test_env_overrides_yaml(monkeypatch):
    monkeypatch.setenv("ARKITEKT_CONFIG_FILE", CONFIG)
    monkeypatch.setenv("POSTGRES__PASSWORD", "from-env")
    monkeypatch.setenv("SYNC__BATCH_SIZE", "17")
    settings = Settings()
    assert settings.postgres.password == "from-env" and settings.sync.batch_size == 17
