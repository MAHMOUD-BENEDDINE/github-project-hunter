from dataclasses import dataclass, field
import math
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    token: str = field(default="", repr=False)
    api_version: str = "2022-11-28"
    cache_ttl: float = 300.0
    max_wait: float = 120.0
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    max_attempts: int = 4
    cache_entries: int = 128
    cache_dir: str = ".cache"
    data_dir: str = ".data"
    persistent_cache_enabled: bool = False
    log_level: str = "INFO"

    def __post_init__(self):
        for name in (
            "cache_ttl",
            "max_wait",
            "connect_timeout",
            "read_timeout",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative.")

        if self.connect_timeout == 0 or self.read_timeout == 0:
            raise ValueError("Timeouts must be greater than zero.")
        if not 1 <= self.max_attempts <= 8:
            raise ValueError("max_attempts must be between 1 and 8.")
        if self.cache_entries < 1:
            raise ValueError("cache_entries must be positive.")

    @classmethod
    def from_env(cls):
        # Explicit environment variables take precedence over .env.
        load_dotenv(Path(__file__).with_name(".env"), override=False)
        return cls(
            token=os.getenv("GITHUB_TOKEN", "").strip(),
            api_version=os.getenv("GITHUB_API_VERSION", "2022-11-28").strip(),
            cache_ttl=float(os.getenv("GITHUB_CACHE_TTL_SECONDS", "300")),
            max_wait=float(os.getenv("GITHUB_MAX_WAIT_SECONDS", "120")),
            connect_timeout=float(os.getenv("GITHUB_CONNECT_TIMEOUT_SECONDS", "5")),
            read_timeout=float(os.getenv("GITHUB_READ_TIMEOUT_SECONDS", "30")),
            max_attempts=int(os.getenv("GITHUB_MAX_ATTEMPTS", "4")),
            cache_entries=int(os.getenv("GITHUB_CACHE_ENTRIES", "128")),
            cache_dir=os.getenv("GITHUB_CACHE_DIR", ".cache").strip(),
            data_dir=os.getenv("GITHUB_DATA_DIR", ".data").strip(),
            persistent_cache_enabled=os.getenv("GITHUB_PERSISTENT_CACHE", "1").strip().lower() in ("1", "true", "yes"),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        )
