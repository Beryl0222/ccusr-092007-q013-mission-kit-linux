"""JSONL 事件存储：追加写入，启动时全量重放。

事件即真相；状态永远可以由日志重算，因此离线补传只需追加行，
重放时按发生时间归位，不会产生额外库存。
"""

from __future__ import annotations

import json
from pathlib import Path

from .events import Event, event_from_dict, event_to_dict


class JsonlEventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, event: Event) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event_to_dict(event), ensure_ascii=False) + "\n")

    def read_all(self) -> list[Event]:
        if not self.path.exists():
            return []
        return [
            event_from_dict(json.loads(line))
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
