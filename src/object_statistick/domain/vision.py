"""Stable domain vocabulary shared by ML components and reports."""

MODEL_CLASSES: dict[int, str] = {0: "Car", 1: "Person", 2: "Technik"}
TRACKED_CLASSES: tuple[str, ...] = tuple(MODEL_CLASSES.values())
