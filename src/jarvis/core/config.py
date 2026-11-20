from pathlib import Path

import keyring
import yaml

ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = ROOT / "config"
DATA = ROOT / "data"


def load_config() -> dict:
    with open(CONFIG_DIR / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_persona() -> str:
    return (CONFIG_DIR / "persona.md").read_text(encoding="utf-8")


def get_secret(name: str) -> str | None:
    """API keys/tokens live in Windows Credential Manager, never in files."""
    return keyring.get_password("jarvis", name)


def ensure_data_dirs() -> None:
    (DATA / "logs").mkdir(parents=True, exist_ok=True)
