"""Local configuration and data paths."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 19004
    data_dir: Path = Path.home() / "Library/Application Support/Clothesline"

    def __post_init__(self):
        if self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Unauthenticated Clothesline must bind to loopback")
        if not 1024 <= self.port <= 65535:
            raise ValueError("Port must be between 1024 and 65535")

    @property
    def database(self) -> Path:
        return self.data_dir / "clothesline.sqlite3"

    @property
    def model_dir(self) -> Path:
        return self.data_dir / "models"


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("CLOTHESLINE_CONFIG", Path.home() / ".config/clothesline/config.toml"))
    settings = tomllib.loads(path.read_text()) if path.exists() else {}
    unknown = set(settings) - {"host", "port", "data_dir"}
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    return Config(
        host=os.environ.get("CLOTHESLINE_HOST", settings.get("host", "127.0.0.1")),
        port=int(os.environ.get("CLOTHESLINE_PORT", settings.get("port", 19004))),
        data_dir=Path(os.environ.get("CLOTHESLINE_DATA_DIR", settings.get("data_dir", Config.data_dir))).expanduser(),
    )
