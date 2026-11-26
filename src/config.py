from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-6-luna", validation_alias="OPENAI_MODEL")
    course_url: str = Field(
        default="https://academy.openai.com/learning-path/ai-foundations-dnq5w/learn/ai-foundations-juzjs/lessons",
        validation_alias="COURSE_URL"
    )
    headless: bool = Field(default=False, validation_alias="HEADLESS")
    slow_mo_ms: int = Field(default=0, validation_alias="SLOW_MO_MS")
    user_data_dir: str = Field(default="./browser_profile", validation_alias="USER_DATA_DIR")
    cdp_url: str = Field(default="", validation_alias="CDP_URL")
    max_steps: int = Field(default=150, validation_alias="MAX_STEPS")
    solve_effort: str = Field(default="high", validation_alias="SOLVE_EFFORT")   # reasoning effort for answering questions
    length_prior: bool = Field(default=True, validation_alias="LENGTH_PRIOR")    # practice questions: break doubt in favour of the longest option
    answer_votes: int = Field(default=3, validation_alias="ANSWER_VOTES")        # independent answers per question (majority wins); 0 = off
    reading_delay_seconds: float = Field(default=0.5, validation_alias="READING_DELAY_SECONDS")
    debug_dir: Path = BASE_DIR / "logs" / "screenshots"

settings = Settings()
