import logging

LOG_FILE ="app.log"
logger = logging.getLogger("zotero_rdf_server")

def setup_logging(log_level="INFO"):
    # logger.setLevel(log_level.upper())
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)
    if not logger.hasHandlers():
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s "
            "[%(filename)s:%(lineno)d in %(funcName)s] %(message)s"
        )

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        file_handler = logging.FileHandler(LOG_FILE)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
