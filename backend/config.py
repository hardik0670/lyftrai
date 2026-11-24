import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_path: Path
    api_keys: tuple[str, ...]
    model: str
    allowed_origins: tuple[str, ...]
    simulate_failures: bool
    api_base: str | None = None
    search_model: str | None = None


def split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def read_settings() -> Settings:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    database_url = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or not database_url[len(prefix):]:
        raise ValueError("DATABASE_URL must be a SQLite file URL, e.g. sqlite:///./data/app.db")

    model = os.getenv("LLM_MODEL", "gemini/gemini-3.5-flash-lite")
    search_model = os.getenv("LLM_SEARCH_MODEL", "gemini/gemini-2.5-flash-lite")
    return Settings(
        database_path=Path(database_url[len(prefix):]),
        api_keys=split_csv(os.getenv("LLM_API_KEYS", "")),
        model=model,
        allowed_origins=split_csv(
            os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
        ),
        simulate_failures=os.getenv("ENABLE_FAILURE_SIMULATION", "false").lower() == "true",
        api_base=os.getenv("LLM_API_BASE") or None,
        search_model=search_model if model.startswith("gemini/") and search_model.startswith("gemini/") else None,
    )
