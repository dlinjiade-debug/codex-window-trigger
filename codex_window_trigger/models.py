from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Episode:
    key: str
    event_id: str
    score: float
    summary: str
    source_url: str
    forecast_updated_at: datetime
    feed_fetched_at: datetime
    target_at: datetime | None
    announced_at: datetime


@dataclass(frozen=True)
class SourceDecision:
    eligible: bool
    reason: str
    episode: Episode | None = None
    valid: bool = False
