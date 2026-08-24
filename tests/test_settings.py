"""Tests for application settings loading."""
from settings import Settings, settings


class TestSettings:
    """Settings load with correct defaults and can be overridden."""

    def test_defaults_are_loaded(self):
        s = Settings()
        assert s.log_level == "INFO"
        assert s.app_port == 5003
        assert s.upload_folder == "uploads"
        assert s.processed_folder == "processed"
        assert s.default_target_language == "da"

    def test_singleton_is_a_settings_instance(self):
        assert isinstance(settings, Settings)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_TARGET_LANGUAGE", "de")
        s = Settings()
        assert s.default_target_language == "de"
