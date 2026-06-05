from __future__ import annotations

import logging
import os, sys
import threading
from datetime import date
from pathlib import Path


class LevelFormatter(logging.Formatter):
    default_msec_format = "%s.%03d"

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        timestamp = super().formatTime(record, datefmt)
        return f"{timestamp}.{int(record.msecs):03d}"

    def format(self, record: logging.LogRecord) -> str:
        record.short_name = record.name.rsplit(".", 1)[-1]
        if record.levelno >= logging.ERROR:
            self._style._fmt = "[%(asctime)s] [%(levelname)s] [%(short_name)s] %(funcName)s -> %(message)s"
        else:
            self._style._fmt = "[%(asctime)s] [%(levelname)s] [%(short_name)s] %(message)s"
        return super().format(record)


class DailyFileHandler(logging.FileHandler):
    _initialized_paths: set[Path] = set()
    _initialized_paths_lock = threading.Lock()

    def __init__(self, directory: str | Path, override: bool = False) -> None:
        self.directory = Path(directory)
        self.override = override
        self.current_date = date.today()
        self.directory.mkdir(parents=True, exist_ok=True)
        super().__init__(self._path_for_date(self.current_date), mode=self._mode())

    def _path_for_date(self, log_date: date) -> Path:
        return self.directory / f"{log_date:%Y-%m-%d}.log"

    def _mode(self) -> str:
        path = self._path_for_date(self.current_date)
        if not self.override:
            return "a"

        with self._initialized_paths_lock:
            if path in self._initialized_paths:
                return "a"
            self._initialized_paths.add(path)
            return "w"

    def emit(self, record: logging.LogRecord) -> None:
        record_date = date.fromtimestamp(record.created)
        if record_date != self.current_date:
            self.current_date = record_date
            if self.stream:
                self.stream.close()
                self.stream = None
            self.baseFilename = str(self._path_for_date(record_date))
        super().emit(record)


def _file_logging_disabled() -> bool:
    disabled = os.getenv("DISABLE_FILE_LOGGING", "").lower()
    if disabled in {"1", "true", "yes", "on"}:
        return True
    if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("PYTEST_VERSION"):
        return True
    argv = " ".join(sys.argv).lower()
    return "unittest" in sys.modules and ("unittest" in argv or "discover" in sys.argv or "/tests/" in argv or "\\tests\\" in argv)


def configure_logger(
    name: str,
    file_path: str | Path | None = None,
    override: bool = True,
    level: int = logging.INFO,
    daily_file: bool = True,
) -> logging.Logger:
    logger = logging.getLogger(name)
    disable_file_logging = _file_logging_disabled()
    if disable_file_logging:
        for handler in list(logger.handlers):
            if isinstance(handler, logging.FileHandler):
                logger.removeHandler(handler)
                handler.close()
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = LevelFormatter(datefmt="%Y-%m-%d %H:%M:%S")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    if not disable_file_logging and file_path is not None and not any(isinstance(handler, logging.FileHandler) for handler in logger.handlers):
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, mode="w" if override else "a")
        formatter = LevelFormatter(datefmt="%Y-%m-%d %H:%M:%S")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    if (
        not disable_file_logging
        and file_path is None
        and daily_file
        and not any(isinstance(handler, logging.FileHandler) for handler in logger.handlers)
    ):
        handler = DailyFileHandler(Path(__file__).resolve().parents[2] / "trading" / "log", override=override)
        formatter = LevelFormatter(datefmt="%Y-%m-%d %H:%M:%S")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger
