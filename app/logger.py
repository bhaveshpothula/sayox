"""Logging setup."""
import logging
import os
import sys

_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def get_logger(name, log_dir=None):
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(sh)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.FileHandler(os.path.join(log_dir, "newsbot.log"))
        fh.setFormatter(logging.Formatter(_FMT))
        logger.addHandler(fh)
    return logger
