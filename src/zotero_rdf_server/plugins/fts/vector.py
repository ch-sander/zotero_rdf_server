from .helpers import ensure_import, clean_ocr, plugin_logger
ensure_import("sentence_transformers==5.2.2", requirements=None)
from sentence_transformers import SentenceTransformer

_model = None

def embed(text: str) -> list[float]:
    global _model
    if _model is None:
        _model = SentenceTransformer("intfloat/multilingual-e5-large") # 1024 dimensions
    vec = _model.encode(
        text,
        normalize_embeddings=True,
        prompt="passage: "
    )
    return vec.tolist()
