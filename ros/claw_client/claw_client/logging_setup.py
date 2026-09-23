import logging
import logging.handlers
import os
import sys
import copy

LEVEL_MAP = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARN': logging.WARNING,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'FATAL': logging.CRITICAL
}

COLORS = {
    'DEBUG': '\033[36m',
    'INFO': '\033[32m',
    'WARNING': '\033[33m',
    'ERROR': '\033[31m',
    'CRITICAL': '\033[35m',
    'RESET': '\033[0m'
}


class ColorFormatter(logging.Formatter):
    def format(self, record):
        record = copy.copy(record)
        color = COLORS.get(record.levelname, COLORS['RESET'])
        record.levelname = f"{color}{record.levelname}{COLORS['RESET']}"
        return super().format(record)


def setup_logging(logger_name: str, console_level: str, file_level: str, log_filename: str = None):
    """
    配置 Python 标准日志（带颜色）

    Returns:
        tuple: (logger, file_logging_enabled, log_file_path)
    """
    if log_filename is None:
        log_filename = f"{logger_name}.log"

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    console_format = '%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - %(message)s'
    file_format = '%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(LEVEL_MAP.get(console_level, logging.INFO))
    console_handler.setFormatter(ColorFormatter(console_format, datefmt=date_format))
    logger.addHandler(console_handler)

    log_dir = "/var/log/cmcc_robot"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, log_filename)

    file_logging_enabled = False
    log_file_path = None

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8'
        )
        file_handler.setLevel(LEVEL_MAP.get(file_level, logging.DEBUG))
        file_handler.setFormatter(logging.Formatter(file_format, datefmt=date_format))
        logger.addHandler(file_handler)
        file_logging_enabled = True
        log_file_path = log_file
    except (IOError, OSError, PermissionError) as e:
        logger.warning(f"无法创建日志文件 {log_file}: {e}")

    logger.propagate = False
    return logger, file_logging_enabled, log_file_path
