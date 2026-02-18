from .helpers import ensure_import, plugin_logger
ensure_import("sentence_transformers==5.2.2", requirements=None)

_model = None
import os, re
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["TQDM_DISABLE"] = "1"

import logging
logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

logger = plugin_logger()

from sentence_transformers import SentenceTransformer
def embed(text: str) -> list[float]:
    global _model
    try:
        if _model is None:
            _model = SentenceTransformer("intfloat/multilingual-e5-large") # 1024 dimensions
        vec = _model.encode(
            text,
            normalize_embeddings=True,
            prompt="passage: ",
            show_progress_bar=False
        )
    except Exception as e:
        logger.error(f"Embedding error: {str(e)}")
    logger.debug(f"Embedding for text with len {len(text)} created")
    return vec.tolist()

# TODO
from typing import Union
VECTOR_CFG = {  "embed": {
    "model": "intfloat/multilingual-e5-large",
    "prompt": "passage: ",
    "normalize": True
        }}

def get_embedder(
    model: Union[str, SentenceTransformer] = "intfloat/multilingual-e5-large",
) -> SentenceTransformer:
    if isinstance(model, SentenceTransformer):
        return model
    return SentenceTransformer(model)


def embed_new(
    text: str,
    *,
    model: Union[str, SentenceTransformer] = "intfloat/multilingual-e5-large",
    prompt: str = "passage: ",
    normalize: bool = True,
) -> list[float]:
    st = get_embedder(model)
    vec = st.encode(
        text,
        normalize_embeddings=normalize,
        prompt=prompt,
        show_progress_bar=False,
    )
    return vec.tolist()