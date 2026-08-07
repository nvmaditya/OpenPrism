from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# monorepo root (backend/app/settings.py → ../../)
_MONOREPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(
            str(_MONOREPO_ROOT / ".env"),
            ".env",
        ),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    prism_llm_provider: str = "groq"
    prism_llm_model: str = "llama-3.3-70b-versatile"
    prism_llm_api_key: str = ""
    prism_llm_base_url: str = ""

    # Groq Cloud (OpenAI-compatible). Read from GROQ_API_KEY in .env
    groq_api_key: str = ""

    prism_projects_root: Path = Path("data/projects")
    prism_preview_root: Path = Path("data/previews")
    prism_skill_home: Path = Path("data/skill-home")

    host: str = "127.0.0.1"
    port: int = 8000


@lru_cache
def get_settings() -> Settings:
    return Settings()
