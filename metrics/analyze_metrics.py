"""Строит почасовые графики персонала и рассчитывает метрики по CSV."""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

SHIFT_SECONDS = 8 * 60 * 60
HEADER = ["class_id", "time", "action", "workers_count", "equipment_count", "cars_count"]


@dataclass(frozen=True)
class Event:
    class_id: int
    seconds: int
    action: str
    counts: tuple[int, int, int]


def to_seconds(value: str) -> int:
    hours, minutes, seconds = map(int, value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def load_events(path: Path) -> list[Event]:
    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != HEADER:
            raise ValueError(f"Unexpected columns in {path}: {reader.fieldnames}")
        events: list[Event] = []
        previous_time = -1
        for row_number, row in enumerate(reader, start=2):
            class_id = int(row["class_id"])
            seconds = to_seconds(row["time"])
            counts = tuple(int(row[column]) for column in HEADER[3:])
            if class_id not in {0, 1, 2} or row["action"] not in {"заехал", "вышел"}:
                raise ValueError(f"Invalid event in {path}, row {row_number}")
            if not 0 <= seconds <= SHIFT_SECONDS or seconds < previous_time or min(counts) < 0:
                raise ValueError(f"Invalid time or counts in {path}, row {row_number}")
            events.append(Event(class_id, seconds, row["action"], counts))
            previous_time = seconds
    if not events:
        raise ValueError(f"No event rows in {path}")
    return events


def time_weighted_average(events: list[Event], class_id: int) -> float:
    current_count, cursor, area = 0, 0, 0
    for event in events:
        area += current_count * (event.seconds - cursor)
        current_count, cursor = event.counts[class_id], event.seconds
    return (area + current_count * (SHIFT_SECONDS - cursor)) / SHIFT_SECONDS


def draw_personnel_chart(events: list[Event], title: str, output: Path) -> None:
    hourly: dict[int, dict[str, int]] = defaultdict(lambda: {"заехал": 0, "вышел": 0})
    for event in events[1:]:  # The first row fixes the initial zero state.
        if event.class_id == 0:
            hourly[event.seconds // 3600][event.action] += 1
    hours = np.arange(8)
    figure, axis = plt.subplots(figsize=(10, 5))
    axis.bar(hours - 0.2, [hourly[hour]["заехал"] for hour in hours], 0.4, label="Вошло")
    axis.bar(hours + 0.2, [hourly[hour]["вышел"] for hour in hours], 0.4, label="Вышло")
    axis.set_xticks(hours, [f"{hour:02d}:00" for hour in hours])
    axis.set_xlabel("Время с начала смены")
    axis.set_ylabel("Количество событий")
    axis.set_title(title)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output, dpi=140)
    plt.close(figure)


def analyse_pair(number: str, data_dir: Path, output_dir: Path) -> dict[str, object]:
    reference = load_events(data_dir / f"{number}.csv")
    prediction = load_events(data_dir / f"{number}_pred.csv")
    draw_personnel_chart(reference, f"{number}: почасовое движение персонала", output_dir / f"{number}_people.png")
    draw_personnel_chart(prediction, f"{number}: предсказанное движение персонала", output_dir / f"{number}_pred_people.png")
    missing_rows = len(reference) - len(prediction)
    if missing_rows < 0:
        raise ValueError(f"Prediction has extra rows: {number}")
    workers_average = time_weighted_average(reference, 0)
    equipment_average = time_weighted_average(reference, 1)
    pred_workers_average = time_weighted_average(prediction, 0)
    pred_equipment_average = time_weighted_average(prediction, 1)
    return {
        "table": number,
        "workers_average": round(workers_average, 3),
        "equipment_average": round(equipment_average, 3),
        "pred_workers_average": round(pred_workers_average, 3),
        "pred_equipment_average": round(pred_equipment_average, 3),
        "missing_rows": missing_rows,
        "missing_rows_share_percent": round(missing_rows / len(reference) * 100, 3),
        "workers_mape_percent": round(abs(pred_workers_average - workers_average) / workers_average * 100, 3),
        "equipment_mape_percent": round(abs(pred_equipment_average - equipment_average) / equipment_average * 100, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("metrics/data"))
    parser.add_argument("--output-dir", type=Path, default=Path("metrics/figures"))
    arguments = parser.parse_args()
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    source_files = sorted(path for path in arguments.data_dir.glob("*.csv") if not path.stem.endswith("_pred"))
    if not source_files:
        raise FileNotFoundError(f"No source CSV files in {arguments.data_dir}")
    results = [analyse_pair(path.stem, arguments.data_dir, arguments.output_dir) for path in source_files]
    with (arguments.output_dir / "summary.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    for result in results:
        print(f"{result['table']}: missing={result['missing_rows_share_percent']}%, workers MAPE={result['workers_mape_percent']}%, equipment MAPE={result['equipment_mape_percent']}%")


if __name__ == "__main__":
    main()
