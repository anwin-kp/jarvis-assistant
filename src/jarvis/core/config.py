from pathlib import Path

import keyring
import yaml

ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = ROOT / "config"
DATA = ROOT / "data"


def load_config() -> dict:
    with open(CONFIG_DIR / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_persona(profile: str | None = None) -> str:
    """Base persona plus an optional profile from config/personas/<profile>.md."""
    base = (CONFIG_DIR / "persona.md").read_text(encoding="utf-8")
    if profile and profile != "default":
        extra = CONFIG_DIR / "personas" / f"{profile}.md"
        if extra.exists():
            base += "\n" + extra.read_text(encoding="utf-8")
    return base


def list_personas() -> list[str]:
    folder = CONFIG_DIR / "personas"
    return ["default"] + sorted(p.stem for p in folder.glob("*.md")) if folder.exists() else ["default"]


def get_secret(name: str) -> str | None:
    """API keys/tokens live in Windows Credential Manager, never in files."""
    return keyring.get_password("jarvis", name)


def ensure_data_dirs() -> None:
    (DATA / "logs").mkdir(parents=True, exist_ok=True)
