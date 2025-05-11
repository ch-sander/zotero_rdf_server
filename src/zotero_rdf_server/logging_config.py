import logging

logger = logging.getLogger("zotero_rdf_server")

def setup_logging(log_level="INFO"):
    # logger.setLevel(log_level.upper())
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)
    if not logger.hasHandlers():
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s "
            "[%(filename)s:%(lineno)d in %(funcName)s] %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
