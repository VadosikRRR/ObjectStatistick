"""Validated, versioned configuration of monitored construction sites."""
from __future__ import annotations

import os
from datetime import time
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class ShiftConfig(BaseModel):
    number: int = Field(ge=1, le=99)
    start: time
    end: time

    @model_validator(mode="after")
    def end_must_be_after_start(self) -> "ShiftConfig":
        if self.end <= self.start:
            raise ValueError("Cross-midnight shifts are not supported; split them into two shifts.")
        return self


class ModelConfig(BaseModel):
    path: str = Field(min_length=1)
    thresholds: dict[str, float] = Field(default_factory=lambda: {"Person": 0.4, "Technik": 0.4, "Car": 0.4})

    @field_validator("thresholds")
    @classmethod
    def validate_thresholds(cls, values: dict[str, float]) -> dict[str, float]:
        allowed = {"Person", "Technik", "Car"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown ML classes: {sorted(unknown)}")
        if any(not 0 <= value <= 1 for value in values.values()):
            raise ValueError("Confidence thresholds must be in [0, 1].")
        return values


class TrackingConfig(BaseModel):
    """ByteTrack parameters. Defaults preserve the project's historical setup."""

    track_activation_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    lost_track_buffer: int = Field(default=90, ge=1, le=10_000)
    minimum_matching_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    frame_rate: int = Field(default=10, ge=1, le=240)


class ProjectConfig(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1)
    timezone: str = "Asia/Yekaterinburg"
    camera_source_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    telegram_bot_token_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    report_attachment_path: str | None = None
    shifts: list[ShiftConfig] = Field(min_length=1)
    roi: tuple[int, int, int, int]
    line: tuple[tuple[int, int], tuple[int, int]]
    line_in_is_entry: bool = True
    frame_skip: int = Field(default=2, ge=1, le=120)
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)
    model: ModelConfig

    @model_validator(mode="after")
    def validate_geometry_and_shifts(self) -> "ProjectConfig":
        x1, y1, x2, y2 = self.roi
        if x2 <= x1 or y2 <= y1:
            raise ValueError("ROI must be (x1, y1, x2, y2) with positive width and height.")
        numbers = [shift.number for shift in self.shifts]
        if len(numbers) != len(set(numbers)):
            raise ValueError("Shift numbers must be unique per project.")
        return self

    def camera_source(self) -> str:
        value = os.environ.get(self.camera_source_env, "").strip()
        if not value:
            raise RuntimeError(f"Environment variable {self.camera_source_env} is not set for project {self.id}.")
        return value

    def telegram_bot_token(self) -> str | None:
        return os.environ.get(self.telegram_bot_token_env or "", "").strip() or None


class ProjectsConfig(BaseModel):
    projects: list[ProjectConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def ids_must_be_unique(self) -> "ProjectsConfig":
        ids = [project.id for project in self.projects]
        if len(ids) != len(set(ids)):
            raise ValueError("Project ids must be unique.")
        return self

    def by_id(self, project_id: str) -> ProjectConfig:
        for project in self.projects:
            if project.id == project_id:
                return project
        raise KeyError(f"Project '{project_id}' is absent from projects config.")


def load_projects_config(path: str | Path) -> ProjectsConfig:
    with Path(path).open(encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file) or {}
    return ProjectsConfig.model_validate(data)
