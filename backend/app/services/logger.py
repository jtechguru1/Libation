import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR = Path("/config/logs")
_LOG_FILE = _LOG_DIR / "libation-web.log"
_MAX_OUTPUT_CHARS = 20_000  # truncate very long CLI output in log entries

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("libation")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    handler = RotatingFileHandler(
        _LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    # The UTC offset is part of the timestamp on purpose. Without it a line reads "02:39:47" and
    # gives the reader no way to tell whether that is their clock or the container's — which is
    # exactly how a container quietly running on UTC gets mistaken for one whose clock is wrong.
    # With TZ set the times are local; either way the line says which.
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %z",
    ))
    logger.addHandler(handler)

    _logger = logger
    return logger


def log_cli(command: str, returncode: int, output: str, elapsed: float) -> None:
    """Log a CLI/bridge call. A non-zero exit is logged at ERROR, with its output.

    It used to log the line at INFO and the output at DEBUG regardless of outcome. That meant a
    failed download produced NOTHING under the log viewer's ERROR filter while a full stack trace
    sat in the same file at DEBUG — so a user whose download failed saw a blank screen and had no
    way to find out why.
    """
    logger = get_logger()
    failed = returncode != 0
    status = "OK" if not failed else f"EXIT {returncode}"
    body = ""
    if output:
        body = output if len(output) <= _MAX_OUTPUT_CHARS else output[:_MAX_OUTPUT_CHARS] + "\n... (truncated)"

    if failed:
        logger.error("[cli] %s → %s (%.1fs)%s", command, status, elapsed,
                     f"\n{body}" if body else "")
    else:
        logger.info("[cli] %s → %s (%.1fs)", command, status, elapsed)
        if body:
            logger.debug("[cli] output:\n%s", body)
