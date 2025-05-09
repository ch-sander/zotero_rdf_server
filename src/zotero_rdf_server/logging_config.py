import logging

logger = logging.getLogger("zotero_rdf_server")

def setup_logging(log_level="INFO"):
    logger.setLevel(log_level.upper())
    if not logger.hasHandlers():
        handler = logging.StreamHandler()
        formatter = logging.Formatter("[%(levelname)s] %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
