import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict


class LeadVolumePoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date: date
    count: int


class IndustryMixPoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    industry: str
    count: int


class ScoreDistributionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    high: int
    medium: int
    low: int
    unscored: int


class ExhibitionPerformancePoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    exhibition_id: uuid.UUID
    exhibition_name: str | None
    lead_count: int
    # avg_score intentionally removed for the time being, until scoring
    # itself is revisited — see .claude/specs/16-dashboard-analytics.md


class RoleMixPoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # Raw VisitingCard.designation_level value ("c_level"/"director"/
    # "manager"/"individual_contributor"), or "Unclassified" for NULL —
    # the frontend maps these to display labels, there is no label map here.
    role: str
    count: int


class RegionMixPoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    region: str
    count: int


class DashboardAnalyticsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    lead_volume: list[LeadVolumePoint]
    industry_mix: list[IndustryMixPoint]
    score_distribution: ScoreDistributionOut
    exhibition_performance: list[ExhibitionPerformancePoint]
    role_mix: list[RoleMixPoint]
    region_mix: list[RegionMixPoint]
