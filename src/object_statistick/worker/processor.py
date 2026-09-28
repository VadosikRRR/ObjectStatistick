"""Pure ML processing. This module has no HTTP, scheduler, or bot dependency."""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import supervision as sv
import torch
from ultralytics import RTDETR, YOLO

from ..config.schemas import ProjectConfig
from ..domain.vision import MODEL_CLASSES, TRACKED_CLASSES

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Event:
    class_name: str
    occurred_at: datetime
    count_after: int
    direction: str


@dataclass(slots=True)
class Counters:
    counts: dict[str, int] = field(default_factory=lambda: {name: 0 for name in TRACKED_CLASSES})
    entries: dict[str, int] = field(default_factory=lambda: {name: 0 for name in TRACKED_CLASSES})
    exits: dict[str, int] = field(default_factory=lambda: {name: 0 for name in TRACKED_CLASSES})
    events: list[Event] = field(default_factory=list)

    def apply(self, class_name: str, direction: str, occurred_at: datetime) -> None:
        if direction == "entry":
            self.counts[class_name] += 1
            self.entries[class_name] += 1
        else:
            # A missed entry must not create a negative occupancy count.
            self.counts[class_name] = max(0, self.counts[class_name] - 1)
            self.exits[class_name] += 1
        self.events.append(Event(class_name, occurred_at, self.counts[class_name], direction))


class VideoProcessor:
    """Runs one self-contained inference job; tracker state never leaks into another shift."""

    def __init__(self, project: ProjectConfig, artifacts_root: Path) -> None:
        self.project = project
        self.artifacts_root = artifacts_root
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        model_path = project.model.path.lower()
        self.model = YOLO("yolo26m.pt")
        # self.model = YOLO(project.model.path) if "yolo" in model_path else RTDETR(project.model.path)

    def process(self, recording_path: Path, started_at: datetime, recording_id: str) -> tuple[dict[str, Any], dict[str, str]]:
        capture = cv2.VideoCapture(str(recording_path))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if not capture.isOpened() or fps <= 0 or width <= 0 or height <= 0:
            capture.release()
            raise RuntimeError(f"Invalid video file: {recording_path}")

        job_dir = self.artifacts_root / self.project.id / recording_id
        job_dir.mkdir(parents=True, exist_ok=True)
        annotated_path = job_dir / "annotated.mp4"
        writer = cv2.VideoWriter(
            str(annotated_path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            max(fps / self.project.frame_skip, 1),
            (width, height),
        )
        if not writer.isOpened():
            capture.release()
            raise RuntimeError(f"Cannot create {annotated_path}")

        x1, y1, x2, y2 = self.project.roi
        line_start = (x1 + self.project.line[0][0], y1 + self.project.line[0][1])
        line_end = (x1 + self.project.line[1][0], y1 + self.project.line[1][1])
        tracking = self.project.tracking
        tracker = sv.ByteTrack(
            track_activation_threshold=tracking.track_activation_threshold,
            lost_track_buffer=tracking.lost_track_buffer,
            minimum_matching_threshold=tracking.minimum_matching_threshold,
            frame_rate=tracking.frame_rate,
        )
        line_zones = {
            class_name: sv.LineZone(
                start=sv.Point(*line_start), end=sv.Point(*line_end),
                triggering_anchors=[sv.Position.CENTER, sv.Position.TOP_CENTER], minimum_crossing_threshold=2,
            )
            for class_name in TRACKED_CLASSES
        }
        previous = {class_name: (0, 0) for class_name in TRACKED_CLASSES}
        counters = Counters()
        frame_number = 0
        last_seconds = 0.0

        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                frame_number += 1
                last_seconds = frame_number / fps
                if frame_number % self.project.frame_skip:
                    continue
                tracked = tracker.update_with_detections(self._detect(frame, x1, y1, x2, y2))
                event_time = started_at + timedelta(seconds=last_seconds)
                self._count_crossings(tracked, line_zones, previous, counters, event_time)
                writer.write(self._annotate(frame, tracked, (x1, y1, x2, y2), line_start, line_end, counters.counts))
        finally:
            writer.release()
            capture.release()

        duration_hours = max(last_seconds / 3600, 1e-6)
        result = self._summarise(counters, started_at, duration_hours)
        events_path = job_dir / "events.csv"
        self._write_events(events_path, counters.events)
        chart_path = self._write_person_chart(job_dir / "personnel_hourly.png", counters.events, started_at, duration_hours)
        artifacts = {"annotated_video": str(annotated_path), "events_csv": str(events_path)}
        if chart_path:
            artifacts["personnel_chart"] = str(chart_path)
        return result, artifacts

    def _detect(self, frame: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> sv.Detections:
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            raise RuntimeError(f"ROI {self.project.roi} is outside input frame {frame.shape[1]}x{frame.shape[0]}")
        device: str | int = 0 if self.device == "cuda" else "cpu"
        result = self.model.predict(crop, verbose=False, device=device)[0]
        detections = sv.Detections.from_ultralytics(result)
        if not len(detections):
            return detections
        mask = np.array([
            int(class_id) in MODEL_CLASSES and float(confidence) >= self.project.model.thresholds.get(MODEL_CLASSES[int(class_id)], 0.4)
            for class_id, confidence in zip(detections.class_id, detections.confidence)
        ], dtype=bool)
        detections = detections[mask]
        if len(detections):
            detections.xyxy[:, [0, 2]] += x1
            detections.xyxy[:, [1, 3]] += y1
            detections.data["class_name"] = np.array([MODEL_CLASSES[int(class_id)] for class_id in detections.class_id], dtype=object)
        return detections

    def _count_crossings(self, tracked: sv.Detections, zones: dict[str, sv.LineZone], previous: dict[str, tuple[int, int]], counters: Counters, occurred_at: datetime) -> None:
        if not len(tracked):
            return
        names = tracked.data.get("class_name", np.array([], dtype=object))
        for class_name in TRACKED_CLASSES:
            mask = np.array([name == class_name for name in names], dtype=bool)
            if not mask.any():
                continue
            zones[class_name].trigger(tracked[mask])
            current_in, current_out = int(zones[class_name].in_count), int(zones[class_name].out_count)
            old_in, old_out = previous[class_name]
            previous[class_name] = (current_in, current_out)
            entered, exited = current_in - old_in, current_out - old_out
            if not self.project.line_in_is_entry:
                entered, exited = exited, entered
            for _ in range(max(entered, 0)):
                counters.apply(class_name, "entry", occurred_at)
            for _ in range(max(exited, 0)):
                counters.apply(class_name, "exit", occurred_at)

    @staticmethod
    def _annotate(frame: np.ndarray, tracked: sv.Detections, roi: tuple[int, int, int, int], line_start: tuple[int, int], line_end: tuple[int, int], counts: dict[str, int]) -> np.ndarray:
        x1, y1, x2, y2 = roi
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.line(frame, line_start, line_end, (0, 255, 255), 2)
        names = tracked.data.get("class_name", [])
        for index, box in enumerate(tracked.xyxy):
            bx1, by1, bx2, by2 = map(int, box)
            name = str(names[index]) if index < len(names) else "object"
            cv2.rectangle(frame, (bx1, by1), (bx2, by2), (255, 0, 0), 2)
            cv2.putText(frame, name, (bx1, max(18, by1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)
        for index, class_name in enumerate(TRACKED_CLASSES):
            cv2.putText(frame, f"{class_name}: {counts[class_name]}", (10, 30 + index * 25), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 255), 2)
        return frame

    @staticmethod
    def _write_events(path: Path, events: list[Event]) -> None:
        with path.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=["class", "occurred_at", "direction", "count_after"])
            writer.writeheader()
            writer.writerows({"class": item.class_name, "occurred_at": item.occurred_at.isoformat(), "direction": item.direction, "count_after": item.count_after} for item in events)

    def _summarise(self, counters: Counters, started_at: datetime, duration_hours: float) -> dict[str, Any]:
        ends_at = started_at + timedelta(hours=duration_hours)
        classes: dict[str, dict[str, int | float]] = {}
        for class_name in TRACKED_CLASSES:
            class_events = [event for event in counters.events if event.class_name == class_name]
            classes[class_name] = {
                "entries": counters.entries[class_name], "exits": counters.exits[class_name],
                "average_hour": round(self._time_weighted_average(class_events, started_at, ends_at), 2),
                "end_count": counters.counts[class_name],
            }
        return {"duration_hours": round(duration_hours, 4), "classes": classes}

    @staticmethod
    def _time_weighted_average(events: list[Event], started_at: datetime, ends_at: datetime) -> float:
        current, cursor, area = 0, started_at, 0.0
        for event in events:
            area += current * (event.occurred_at - cursor).total_seconds()
            current, cursor = event.count_after, event.occurred_at
        area += current * (ends_at - cursor).total_seconds()
        seconds = (ends_at - started_at).total_seconds()
        return area / seconds if seconds else 0.0

    @staticmethod
    def _write_person_chart(path: Path, events: list[Event], started_at: datetime, duration_hours: float) -> Path | None:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except Exception:
            return None
        end = started_at + timedelta(hours=duration_hours)
        hourly: dict[str, dict[str, int]] = {}
        for event in events:
            if event.class_name != "Person":
                continue
            label = event.occurred_at.strftime("%H:00")
            hourly.setdefault(label, {"entry": 0, "exit": 0})[event.direction] += 1
        labels = list(hourly) or [started_at.strftime("%H:00")]
        entries = [hourly.get(label, {}).get("entry", 0) for label in labels]
        exits = [hourly.get(label, {}).get("exit", 0) for label in labels]
        positions = np.arange(len(labels))
        figure, axis = plt.subplots(figsize=(10, 5))
        axis.bar(positions - 0.2, entries, 0.4, label="Вошло")
        axis.bar(positions + 0.2, exits, 0.4, label="Вышло")
        axis.set_xticks(positions, labels)
        axis.set_title(f"{started_at:%d.%m.%Y}: почасовое движение персонала")
        axis.legend()
        figure.tight_layout()
        figure.savefig(path, dpi=140)
        plt.close(figure)
        return path
