import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("utc_time", "elapsed_s", "received_monotonic_s", "participant", "trial",
          "condition", "route", "event", "side", "point", "direction",
          "expected_action", "input", "result", "reason", "before", "after",
          "device_ms", "sequence", "reference", "details")

class EventLog:
    def __init__(self, directory, participant, trial, condition, route, echo=True):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        self.directory = root / f"{stamp}_{participant}_{trial}_{condition}"
        self.directory.mkdir()
        self.handle = (self.directory / "events.csv").open("x", encoding="utf-8-sig", newline="")
        self.writer = csv.DictWriter(self.handle, FIELDS)
        self.writer.writeheader()
        self.handle.flush()
        self.context = dict(participant=participant, trial=trial, condition=condition, route=route)
        self.origin = None
        self.echo = echo

    def write(self, event, received=None, **fields):
        now = time.monotonic() if received is None else received
        row = dict.fromkeys(FIELDS, "")
        row.update(self.context, utc_time=datetime.now(timezone.utc).isoformat(),
                   received_monotonic_s=f"{now:.6f}", event=event)
        if self.origin is not None:
            row["elapsed_s"] = f"{now - self.origin:.6f}"
        for key, value in fields.items():
            if key not in row:
                raise ValueError(f"Unknown log field: {key}")
            row[key] = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
        self.writer.writerow(row)
        self.handle.flush()
        if self.echo:
            print(f"[{row['elapsed_s'] or 'READY':>10}] {event:20} {row['side']:5} {row['point']:10} "
                  f"{row['input']} {row['result']} {row['reason']}", flush=True)

    def snapshot(self, settings, route):
        (self.directory / "session.json").write_text(
            json.dumps({"metadata": self.context, "settings": settings, "route": route},
                       ensure_ascii=False, indent=2), encoding="utf-8")

    def close(self):
        self.handle.close()
