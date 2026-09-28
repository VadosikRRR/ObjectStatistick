"""RTSP recording as a bounded background operation owned by Backend."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import cv2

LOGGER = logging.getLogger(__name__)


class RTSPRecorder:
    """Records one finite shift and reconnects without exposing credentials in logs."""

    def __init__(self, source: str, destination: Path, until: datetime, on_finish: Callable[[Path | None, str | None], None]) -> None:
        self._source = source
        self._destination = destination
        self._until = until
        self._on_finish = on_finish
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"recorder-{self._destination.stem}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        capture: cv2.VideoCapture | None = None
        writer: cv2.VideoWriter | None = None
        errors = 0
        try:
            self._destination.parent.mkdir(parents=True, exist_ok=True)
            while not self._stop.is_set() and datetime.now(self._until.tzinfo) < self._until:
                if capture is None or not capture.isOpened():
                    if capture is not None:
                        capture.release()
                    capture = cv2.VideoCapture(self._source, cv2.CAP_FFMPEG)
                    capture.set(cv2.CAP_PROP_BUFFERSIZE, 3)
                    if not capture.isOpened():
                        LOGGER.warning("Camera is unavailable; retrying in 2 seconds.")
                        time.sleep(2)
                        continue
                ok, frame = capture.read()
                if not ok:
                    errors += 1
                    if errors >= 30:
                        capture.release()
                        capture = None
                        errors = 0
                    time.sleep(0.05)
                    continue
                errors = 0
                if writer is None:
                    height, width = frame.shape[:2]
                    fps = capture.get(cv2.CAP_PROP_FPS)
                    fps = fps if 1 <= fps <= 60 else 25.0
                    writer = cv2.VideoWriter(str(self._destination), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
                    if not writer.isOpened():
                        raise RuntimeError(f"Cannot create video file {self._destination}")
                writer.write(frame)
            if writer is None or not self._destination.exists() or self._destination.stat().st_size == 0:
                self._on_finish(None, "No frames were recorded during the shift.")
            else:
                self._on_finish(self._destination, None)
        except Exception as exc:
            LOGGER.exception("Recording failed")
            self._on_finish(None, str(exc))
        finally:
            if writer is not None:
                writer.release()
            if capture is not None:
                capture.release()
