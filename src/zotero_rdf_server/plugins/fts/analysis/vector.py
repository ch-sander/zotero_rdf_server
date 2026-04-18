from ..helpers import ensure_import, plugin_logger, resolve_config_path

import os
import logging

logger = plugin_logger()


from typing import Any
from functools import lru_cache
from pathlib import Path

@lru_cache(maxsize=8)
def load_st_model(model_name: str | None = None):
    if model_name:
        try:
            ensure_import("sentence_transformers==5.2.2", requirements=None)
            from sentence_transformers import SentenceTransformer
            logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
            logging.getLogger("transformers").setLevel(logging.ERROR)
            logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
            os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
            os.environ["TQDM_DISABLE"] = "1"        
            return SentenceTransformer(model_name)
        except:
            return None
    
@lru_cache(maxsize=8)
def load_fasttext_model(model_path: str | Path | None = None):
    if Path(model_path).exists():
        ensure_import("fasttext==0.9.3", requirements=None)
        import fasttext
        return fasttext.load_model(str(model_path))
    return None

@lru_cache(maxsize=8)
def get_vector_config(config_path: Path) -> dict[str, Any]:
    from zotero_rdf_server.utils import load_dict_like
    cfg = load_dict_like(config_path,label="Vector Config",verbose=True)
    return cfg.get("vector") or cfg


def embed(text: str, *, framework: str | None = None, config_path: str | Path | None = None) -> list[float]:
    try:
        vcfg = get_vector_config(resolve_config_path(config_path))
        fw = (framework or "").lower().strip()
        emcfg = vcfg.get("embed") or {}
        frcfg = emcfg.get(framework) or {}
        if fw == "sentencetransformer":
            st_model = frcfg.get("model", "intfloat/multilingual-e5-large")
            model = load_st_model(st_model)
            if model:
                encode_kwargs = frcfg.get('encode_kwargs')
                if encode_kwargs:
                    vec = model.encode(
                        text,
                        **encode_kwargs
                    )
                else:
                    vec = model.encode(
                        text,
                        normalize_embeddings=True,
                        prompt="passage: ",
                        show_progress_bar=False,
                    )
        elif fw == "fasttext":            
            ft_model_path = frcfg.get("model","model.bin")
            model = load_fasttext_model(ft_model_path)
            if model:
                get_sentence_vector_kwargs = frcfg.get('get_sentence_vector_kwargs')
                vec = model.get_sentence_vector(text, **get_sentence_vector_kwargs) if get_sentence_vector_kwargs else model.get_sentence_vector(text)        
        else:
            logger.error(f"No framework (={framework}) for embedding!")
            return []
        logger.debug(f"{framework} embedding for text with len {len(text)} created")
        return vec.tolist()
    except Exception as e:
        logger.error(f"Embedding error: {str(e)}")

    return []