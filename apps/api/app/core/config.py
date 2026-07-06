from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: str = "development"
    database_url: str = "postgresql+psycopg://dashr:dashr@localhost:5432/dashr"
    jwt_secret: str
    jwt_expire_minutes: int = 60 * 24 * 7
    cors_origins_raw: str = Field(default="http://localhost:3000", alias="CORS_ORIGINS")
    otp_expire_minutes: int = 10
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 30
    cookie_secure: bool = False

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins_raw.split(",") if origin.strip()]


settings = Settings()
