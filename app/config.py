"""Runtime configuration, read from environment variables prefixed with ``PRELO_``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MB = 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRELO_", env_file=".env", extra="ignore")

    # Limits
    max_upload_mb: float = Field(20, gt=0, le=1024, description="Maximum upload size (MB).")
    max_concurrent_jobs: int = Field(4, ge=1, le=64, description="Simultaneous conversions.")
    conversion_timeout: float = Field(120, gt=0, description="Per-conversion timeout (seconds).")
    zip_max_files: int = Field(1000, ge=1, description="Maximum number of entries in a .zip.")
    zip_max_uncompressed_mb: float = Field(200, gt=0, description="Max extracted .zip size (MB).")

    # Remote resources (images referenced by URL)
    allow_remote_images: bool = Field(True, description="Download images referenced by URL.")
    allow_private_networks: bool = Field(
        False, description="Allow fetching from private/loopback/link-local addresses."
    )
    remote_image_max_mb: float = Field(10, gt=0, description="Max size of one image (MB).")
    remote_timeout: float = Field(10, gt=0, description="Timeout for one remote fetch (seconds).")
    max_remote_images: int = Field(50, ge=0, description="Max remote images per document.")
    max_images_total_mb: float = Field(
        50, gt=0, description="Max total size of the images/resources of one document (MB)."
    )

    # HTTP
    cors_origins: str = Field("*", description="Comma-separated list of allowed CORS origins.")
    root_path: str = Field("", description="ASGI root_path when served behind a path prefix.")
    log_level: str = Field("info", description="Python log level.")

    # Browser
    chromium_sandbox: Literal["auto", "true", "false"] = Field(
        "auto",
        description=(
            "Chromium's sandbox: 'auto' uses it when the container allows it (see the "
            "seccomp profile), 'true' requires it, 'false' disables it."
        ),
    )

    # External tools / assets
    pandoc_path: str = Field("pandoc", description="pandoc executable.")
    vendor_dir: Path = Field(Path("vendor"), description="Directory with Mermaid / Swagger UI.")
    reference_docx: Path | None = Field(
        None, description="Word reference document; generated automatically when missing."
    )

    @field_validator("chromium_sandbox", mode="before")
    @classmethod
    def _normalize_switch(cls, value: object) -> object:
        aliases = {"1": "true", "yes": "true", "on": "true", "0": "false", "no": "false"}
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            lowered = value.strip().lower()
            return aliases.get(lowered, "false" if lowered == "off" else lowered)
        return value

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * MB)

    @property
    def zip_max_uncompressed_bytes(self) -> int:
        return int(self.zip_max_uncompressed_mb * MB)

    @property
    def remote_image_max_bytes(self) -> int:
        return int(self.remote_image_max_mb * MB)

    @property
    def max_images_total_bytes(self) -> int:
        return int(self.max_images_total_mb * MB)

    @property
    def mermaid_js_path(self) -> Path:
        return self.vendor_dir / "mermaid" / "mermaid.min.js"

    @property
    def swagger_ui_dir(self) -> Path:
        return self.vendor_dir / "swagger-ui"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
