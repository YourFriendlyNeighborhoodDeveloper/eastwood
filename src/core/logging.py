import logging.config
import os
import yaml

def setup_logging(path: str = "config/logging.yaml") -> None:
    if os.path.exists(path):
        with open(path, "r") as f:
            cfg = yaml.safe_load(f) or {}
        logging.config.dictConfig(cfg)
        return

    logging.basicConfig(level=logging.INFO)