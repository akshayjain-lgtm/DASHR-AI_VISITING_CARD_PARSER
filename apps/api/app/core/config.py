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
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_bucket_name: str = "dashr-cards"
    s3_access_key_id: str = "minioadmin"
    s3_secret_access_key: str = "minioadmin"
    s3_region: str = "us-east-1"
    max_upload_file_size_mb: int = 10
    max_bulk_upload_files: int = 200
    allowed_card_image_content_types_raw: str = Field(
        default="image/jpeg,image/png,image/webp",
        alias="ALLOWED_CARD_IMAGE_CONTENT_TYPES",
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins_raw.split(",") if origin.strip()]

    @property
    def allowed_card_image_content_types(self) -> set[str]:
        return {
            t.strip()
            for t in self.allowed_card_image_content_types_raw.split(",")
            if t.strip()
        }

    @property
    def max_upload_file_size_bytes(self) -> int:
        return self.max_upload_file_size_mb * 1024 * 1024


settings = Settings()
