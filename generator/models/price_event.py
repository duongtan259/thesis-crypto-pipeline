from datetime import datetime, timezone
from uuid import uuid4
from pydantic import BaseModel, model_validator
import json


class PriceEvent(BaseModel):
    event_id: str = ""
    symbol: str
    price: float
    volume_24h: float
    market_cap: float = 0.0
    timestamp_utc: datetime
    source: str
    sequence: int = 0

    @model_validator(mode="after")
    def set_event_id(self):
        if not self.event_id:
            self.event_id = str(uuid4())
        return self

    def to_json_bytes(self) -> bytes:
        d = self.model_dump()
        d["timestamp_utc"] = self.timestamp_utc.isoformat()
        # Snapshot the source event before adding ingestion metadata
        source_snapshot = json.dumps(d)
        d["ingestion_time"] = datetime.now(timezone.utc).isoformat()
        d["raw_payload"] = source_snapshot
        return json.dumps(d).encode("utf-8")
