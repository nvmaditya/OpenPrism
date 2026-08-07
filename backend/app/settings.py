from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    prism_llm_provider: str = "openai"
    prism_llm_model: str = "gpt-4o"
    prism_llm_api_key: str = ""
    prism_llm_base_url: str = ""

    prism_projects_root: Path = Path("data/projects")
    prism_preview_root: Path = Path("data/previews")
    prism_skill_home: Path = Path("data/skill-home")

    host: str = "127.0.0.1"
    port: int = 8000


def get_settings() -> Settings:
    return Settings()
