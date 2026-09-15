from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class TrackingTarget:
    platform: str
    product_id: str
    # 同一商品可能有多个运营记录；去重采集但保留每个记录独立的推送开关/接收人。
    records: list[dict] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.product_id}"


class RecordSource(Protocol):
    def read_records(self) -> list[dict]: ...


class Collector(Protocol):
    def collect(self, targets: list[TrackingTarget]) -> dict[str, dict]: ...


class ResultSink(Protocol):
    def write(self, result: dict) -> None: ...
