"""Application configuration, loaded from environment variables or a .env file."""
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file."""

    # Application settings
    log_level: str = "INFO"
    app_port: int = 5003

    # File management
    upload_folder: str = "uploads"
    processed_folder: str = "processed"
    max_upload_bytes: int = 50 * 1024 * 1024

    # Retention cleanup
    file_retention_days: float = 7.0
    cleanup_interval_hours: float = 6.0
    job_grace_period_minutes: float = 15.0

    # Persistence
    database_path: str = "data/glossary.db"

    # Translation settings
    libretranslate_url: str = "http://libretranslate:5000/translate"
    libretranslate_languages_url: str = "http://libretranslate:5000/languages"
    default_target_language: str = "da"
    glossary_enabled: bool = True

    # HTTP client settings
    http_timeout: float = 30.0
    http_connect_timeout: float = 10.0
    max_retries: int = 3
    max_connections: int = 10
    max_keepalive_connections: int = 5

    # Rate limiting (see middleware.py)
    rate_limit_enabled: bool = True
    rate_limit_default: str = "120/minute"
    rate_limit_upload: str = "5/minute"
    rate_limit_storage_uri: str = "memory://"
    rate_limit_trust_forwarded_for: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

# Load settings once at import time
settings = Settings()
