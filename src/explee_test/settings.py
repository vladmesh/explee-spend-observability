from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings; secrets belong in the environment, never in source control."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="EXPLEE_", extra="ignore")

    api_base: str = "https://jobs.explee.com/ai-native-developer/test/api"
    database_path: Path = Path("data/raw.sqlite3")
    alerts_path: Path = Path("alerts.jsonl")
    poll_interval_seconds: int = Field(default=60, ge=5)
    request_timeout_seconds: float = Field(default=15, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
