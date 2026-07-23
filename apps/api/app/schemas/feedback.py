from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FeedbackCreate(BaseModel):
    what_worked: str | None = Field(default=None, max_length=2000)
    what_went_wrong: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> "FeedbackCreate":
        worked = (self.what_worked or "").strip()
        went_wrong = (self.what_went_wrong or "").strip()
        if not worked and not went_wrong:
            raise ValueError("At least one of what_worked or what_went_wrong is required")
        return self


class SupportQueryCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=2000)


class SupportQueryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticket_id: str
    created_at: datetime
