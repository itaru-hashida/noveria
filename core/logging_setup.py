import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging():
    root = logging.getLogger()
    if any(getattr(handler, '_noweria', False) for handler in root.handlers):
        return
    Path('logs').mkdir(exist_ok=True)
    formatter = logging.Formatter('%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s')
    for handler in (RotatingFileHandler('logs/game.log', maxBytes=5*1024**2, backupCount=3, encoding='utf-8'),
                    logging.StreamHandler()):
        handler.setFormatter(formatter)
        handler._noweria = True
        root.addHandler(handler)
    root.setLevel(logging.DEBUG)
