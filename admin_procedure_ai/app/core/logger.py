# app/core/logger.py
import sys

from loguru import logger

from app.core.config import settings


def configure_logging() -> None:
    logger.remove()

    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    log_level = "DEBUG" if settings.DEBUG else "INFO"

    logger.add(
        sys.stderr,
        format=log_format,
        level=log_level,
        colorize=True,
        backtrace=True,
        diagnose=settings.DEBUG,
    )

    logger.add(
        "logs/app.log",
        format=log_format,
        level="INFO",
        rotation="50 MB",
        retention="30 days",
        compression="gz",
        backtrace=True,
        diagnose=False,
        enqueue=True,
    )

    logger.add(
        "logs/error.log",
        format=log_format,
        level="ERROR",
        rotation="20 MB",
        retention="60 days",
        compression="gz",
        backtrace=True,
        diagnose=True,
        enqueue=True,
    )
