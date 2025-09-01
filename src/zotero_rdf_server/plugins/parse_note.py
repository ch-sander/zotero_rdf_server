import subprocess
import sys, json, html
from zotero_rdf_server.logging_config import logger


try:
    from semantic_html.parser import parse_note
except ImportError:
    logger.warning("semantic-html not found. Installing...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "https://github.com/ch-sander/semantic-html/releases/download/v0.5.3/semantic_html-0.5.3-py3-none-any.whl"])
    # semantic-html 
    # semantic-html git+https://github.com/ch-sander/semantic-html.git
    # https://github.com/ch-sander/semantic-html/releases/download/v0.2.0/semantic_html-0.5.3-py3-none-any.whl
    try:
        from semantic_html.parser import parse_note
    except ImportError:
        logger.error("semantic-html could not be imported after installation.")
        raise


class ParseNotePlugin:
    def __init__(self, mapping: dict | None, metadata: dict = None):
        self.mapping = mapping
        self.metadata = metadata or {}
        if not mapping:
            logger.error("No config for parser provided.")
            raise 

    def run(
        self,
        html_str: str,
        note_uri: str,
        rdfa:bool=False,
        wadm:bool=False
    ) -> dict:
        logger.debug(f"Parsing HTML note for URI: {note_uri}")
        logger.debug(f"Unescaping HTML")

        html_str = html.unescape(html_str)

        result = parse_note(
            html_input=html_str,
            mapping=self.mapping,
            note_uri=note_uri,
            metadata=self.metadata,
            rdfa=rdfa,
            wadm=wadm
        )
        logger.debug("Parsing completed.")
        return result
