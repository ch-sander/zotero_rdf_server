from .helpers import ensure_import
ensure_import("sentence_transformers==5.2.2", requirements=None)

_model = None
import os
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["TQDM_DISABLE"] = "1"

import logging
logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

from sentence_transformers import SentenceTransformer
def embed(text: str) -> list[float]:
    global _model
    if _model is None:
        _model = SentenceTransformer("intfloat/multilingual-e5-large") # 1024 dimensions
    vec = _model.encode(
        text,
        normalize_embeddings=True,
        prompt="passage: ",
        show_progress_bar=False
    )
    return vec.tolist()
