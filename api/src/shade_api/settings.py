"""API configuration from environment variables.

Every knob is a ``SHADE_API_*`` environment variable (12-factor style): the
same process serves dev and prod, only the environment changes. Tests build
the settings object programmatically and hand it to ``create_app``.
"""

from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ApiSettings(BaseSettings):
    """Runtime configuration for the shade API."""

    model_config = SettingsConfigDict(env_prefix="SHADE_API_")

    cities_dir: Path = Path("cities")
    artifacts_root: Path = Path("data/cities")
    artifact_version: str = "v1"
    # CSV in the environment (SHADE_API_CORS_ORIGINS="https://a.example,https://b.example").
    # NoDecode is required: pydantic-settings JSON-decodes list fields *before*
    # validators run, so a plain CSV string would be rejected without it.
    cors_origins: Annotated[list[str], NoDecode] = []
    rate_limit: str = "60/minute"
    rate_limit_enabled: bool = True
    block_size: int = 64
    max_cached_blocks: int = 64

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value
