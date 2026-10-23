"""Configuration persistence: config.json next to the package root."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

DEFAULT_PORT = 11112
DEFAULT_AE_TITLE = "DICOMSTORE"
DEFAULT_WEB_PORT = 8080

CONFIG_PATH = Path(
    os.environ.get("DICOMSTORE_CONFIG", Path(__file__).resolve().parent.parent / "config.json")
)


@dataclass
class Config:
    port: int = DEFAULT_PORT
    ae_title: str = DEFAULT_AE_TITLE
    storage_path: str = str(Path(__file__).resolve().parent.parent / "data")
    web_port: int = DEFAULT_WEB_PORT


def load() -> Config:
    """Load config, falling back to defaults if missing or corrupt."""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return Config(
            port=int(data.get("port", DEFAULT_PORT)),
            ae_title=str(data.get("ae_title", DEFAULT_AE_TITLE)),
            storage_path=str(data.get("storage_path", "")) or str(
                Path(__file__).resolve().parent.parent / "data"
            ),
            web_port=int(data.get("web_port", DEFAULT_WEB_PORT)),
        )
    except (OSError, ValueError, TypeError):
        return Config()


def save(config: Config) -> None:
    """Write config atomically (temp file + os.replace)."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONFIG_PATH.parent, prefix=".config_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(config), f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
