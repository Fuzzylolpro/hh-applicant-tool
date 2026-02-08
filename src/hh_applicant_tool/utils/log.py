import enum
import logging
import re
from collections import deque
from datetime import datetime
from enum import auto
from logging.handlers import RotatingFileHandler
from os import PathLike
from typing import Callable, TextIO

# 10MB
MAX_LOG_SIZE = 10 << 20


class Color(enum.Enum):
    BLACK = 30
    RED = auto()
    GREEN = auto()
    YELLOW = auto()
    BLUE = auto()
    PURPLE = auto()
    CYAN = auto()
    WHITE = auto()

    def __str__(self) -> str:
        return str(self.value)


class ColorHandler(logging.StreamHandler):
    _color_map = {
        "CRITICAL": Color.RED,
        "ERROR": Color.RED,
        "WARNING": Color.RED,
        "INFO": Color.GREEN,
        "DEBUG": Color.BLUE,
    }

    def format(self, record: logging.LogRecord) -> str:
        # Подавляем вывод подробного сообщения об ошибке
        orig_exc_info = record.exc_info

        # Детали ошибки показываем только при отладке
        if self.level > logging.DEBUG:
            record.exc_info = None

        message = super().format(record)

        # Восстанавливаем оригинальный exc_info
        record.exc_info = orig_exc_info

        color_code = self._color_map.get(record.levelname, Color.WHITE)
        return f"\033[{color_code}m{message}\033[0m"


class RedactingFilter(logging.Filter):
    def __init__(
            self,
            patterns: list[str],
            placeholder: str | Callable = lambda m: "*" * len(m.group(0)),
    ):
        super().__init__()
        self.pattern = (
            re.compile(f"({'|'.join(patterns)})") if patterns else None
        )
        self.placeholder = placeholder

    def filter(self, record: logging.LogRecord) -> bool:
        if self.pattern:
            msg = record.getMessage()
            msg = self.pattern.sub(self.placeholder, msg)
            record.msg, record.args = msg, ()
        return True


def setup_logger(
        logger: logging.Logger,
        verbosity_level: int,
        log_file: PathLike,
) -> None:
    # Пишем всё
    logger.setLevel(logging.DEBUG)

    # ===== Console (color) handler =====
    color_handler = ColorHandler()
    color_handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] [%(levelname).1s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    color_handler.setLevel(verbosity_level)

    # ===== File handler =====
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=MAX_LOG_SIZE,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    file_handler.setLevel(logging.DEBUG)

    # ===== Redaction filter =====
    redactor = RedactingFilter(
        [
            r"\b[A-Z0-9]{64,}\b",
            r"\b[a-fA-F0-9]{32,}\b",  # request_id, resume_id
        ]
    )
    file_handler.addFilter(redactor)

    # ===== Attach handlers =====
    logger.addHandler(color_handler)
    logger.addHandler(file_handler)


TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def collect_traceback_logs(
        fp: TextIO,
        after_dt: datetime,
        maxlines: int = 1000,
) -> str:
    error_lines = deque(maxlen=maxlines)
    prev_line = ""
    log_dt = None
    collecting_traceback = False

    for line in fp:
        if ts_match := TS_RE.match(line):
            log_dt = datetime.strptime(
                ts_match.group(0), "%Y-%m-%d %H:%M:%S"
            )
            collecting_traceback = False

        if (
                line.startswith("Traceback (most recent call last):")
                and log_dt
                and log_dt >= after_dt
        ):
            error_lines.append(prev_line)
            collecting_traceback = True

        if collecting_traceback:
            error_lines.append(line)

        prev_line = line

    return "".join(error_lines)
