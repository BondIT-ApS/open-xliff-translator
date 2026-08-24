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

    # Translation settings
    libretranslate_url: str = "http://libretranslate:5000/translate"
    libretranslate_languages_url: str = "http://libretranslate:5000/languages"
    default_target_language: str = "da"

    # HTTP client settings
    http_timeout: float = 30.0
    http_connect_timeout: float = 10.0
    max_retries: int = 3
    max_connections: int = 10
    max_keepalive_connections: int = 5

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


# Load settings once at import time
settings = Settings()
