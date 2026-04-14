import os
import logging
from pathlib import Path

from config import LOGS_DIR


class PerLoggerFileHandler(logging.Handler):
    """
    Routes each log record to logs/<logger_name>.log
    Creates file handlers lazily and keeps them cached.
    """
    def __init__(self, logs_dir: str | os.PathLike | Path = LOGS_DIR , *, encoding: str = "utf-8"):
        super().__init__()
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.encoding = encoding
        self._handlers: dict[str, logging.FileHandler] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            name = record.name or "root"
            safe = name.replace("/", "_").replace("\\", "_")
            handler = self._handlers.get(safe)
            if handler is None:
                path = self.logs_dir / f"{safe}.log"
                handler = logging.FileHandler(path, encoding=self.encoding)
                handler.setLevel(self.level)                                
                handler.setFormatter(self.formatter)
                self._handlers[safe] = handler

            handler.emit(record)
        except Exception: self.handleError(record)

    def close(self) -> None:
        for h in self._handlers.values():
            try: h.close()
            except Exception: pass
        self._handlers.clear()
        super().close()


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)

    ph = PerLoggerFileHandler("logs")
    ph.setLevel(logging.INFO)
    ph.setFormatter(fmt)

    root.handlers.clear()
    root.addHandler(sh)
    root.addHandler(ph)
