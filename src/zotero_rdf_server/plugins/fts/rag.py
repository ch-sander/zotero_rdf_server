from .helpers import ensure_import, resolve_config_path, plugin_logger
from zotero_rdf_server.utils import load_dict_like

from typing import Any, Dict
from pathlib import Path
from functools import lru_cache
from copy import deepcopy
import asyncio
import os

ensure_import("lightrag-hku==1.4.14", requirements=None)
# ensure_import("numpy", requirements=None)
def patch_lightrag():
    path = Path("/usr/local/lib/python3.11/site-packages/lightrag/kg/opensearch_impl.py")
    if not path.exists():
        return

    text = path.read_text()

    if "_shard_doc" in text:
        text = text.replace('"_shard_doc"', '"_doc"')
        path.write_text(text)
        print("Patched lightrag (_shard_doc → _doc)")

patch_lightrag()
from lightrag import LightRAG, QueryParam
from lightrag.utils import wrap_embedding_func_with_attrs
from lightrag.llm.ollama import ollama_model_complete, ollama_embed

logger = plugin_logger()


DEFAULT_RAG_CONFIG: dict[str, Any] = {
    "working_dir": "./data/lightrag",
    "workspace": "default",
    "storages": {
        "kv": "OpenSearchKVStorage",
        "vector": "OpenSearchVectorDBStorage",
        "graph": "OpenSearchGraphStorage",
        "doc_status": "OpenSearchDocStatusStorage",
    },
    "opensearch": {
        "hosts": "localhost:9200",
        "user": "admin",
        "password": "",
        "use_ssl": True,
        "verify_certs": False,
        # optional:
        # "timeout": 30,
        # "max_retries": 3,
        # "number_of_shards": 1,
        # "number_of_replicas": 0,
        # "knn_ef_construction": 200,
        # "knn_m": 16,
        # "knn_ef_search": 100,
        # "use_ppl_graphlookup": True,
    },
    "llm": {
        "binding": "ollama",
        "host": "http://localhost:11434",
        "model": "qwen2.5:7b",
        "options": {
            "num_ctx": 32768,
        },
    },
    "embedding": {
        "binding": "ollama",
        "host": "http://localhost:11434",
        "model": "nomic-embed-text",
        "dim": 768,
        "max_token_size": 8192,
    },
    "query": {
        "mode": "mix",
        "only_need_context": False,
        "only_need_prompt": False,
        "response_type": "Multiple Paragraphs",
        "top_k": 40,
        "enable_rerank": True,
    },
}


@lru_cache(maxsize=8)
def get_rag_config(config_path: Path | str | None = None) -> dict[str, Any]:
    if isinstance(config_path, Path):
        config_path = str(config_path)
    cfg_path = resolve_config_path(config_path)
    cfg = load_dict_like(cfg_path, label="LightRAG Config", verbose=False)
    return cfg.get("rag") or cfg


def _deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    result = deepcopy(base)
    if not isinstance(override, dict):
        return result

    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def build_rag_config(ragcfg: dict[str, Any] | None) -> dict[str, Any]:
    cfg = _deep_merge(DEFAULT_RAG_CONFIG, ragcfg or {})

    if not isinstance(cfg.get("working_dir"), str) or not cfg["working_dir"].strip():
        cfg["working_dir"] = DEFAULT_RAG_CONFIG["working_dir"]

    if not isinstance(cfg.get("workspace"), str) or not cfg["workspace"].strip():
        cfg["workspace"] = DEFAULT_RAG_CONFIG["workspace"]

    llm_cfg = cfg.get("llm") or {}
    emb_cfg = cfg.get("embedding") or {}
    os_cfg = cfg.get("opensearch") or {}
    storages = cfg.get("storages") or {}
    query_cfg = cfg.get("query") or {}

    if not isinstance(llm_cfg.get("model"), str) or not llm_cfg["model"].strip():
        llm_cfg["model"] = DEFAULT_RAG_CONFIG["llm"]["model"]
    if not isinstance(llm_cfg.get("host"), str) or not llm_cfg["host"].strip():
        llm_cfg["host"] = DEFAULT_RAG_CONFIG["llm"]["host"]
    if not isinstance(llm_cfg.get("options"), dict):
        llm_cfg["options"] = deepcopy(DEFAULT_RAG_CONFIG["llm"]["options"])

    if not isinstance(emb_cfg.get("model"), str) or not emb_cfg["model"].strip():
        emb_cfg["model"] = DEFAULT_RAG_CONFIG["embedding"]["model"]
    if not isinstance(emb_cfg.get("host"), str) or not emb_cfg["host"].strip():
        emb_cfg["host"] = DEFAULT_RAG_CONFIG["embedding"]["host"]
    if not isinstance(emb_cfg.get("dim"), int) or emb_cfg["dim"] <= 0:
        emb_cfg["dim"] = DEFAULT_RAG_CONFIG["embedding"]["dim"]
    if not isinstance(emb_cfg.get("max_token_size"), int) or emb_cfg["max_token_size"] <= 0:
        emb_cfg["max_token_size"] = DEFAULT_RAG_CONFIG["embedding"]["max_token_size"]

    if not isinstance(os_cfg.get("hosts"), str) or not os_cfg["hosts"].strip():
        os_cfg["hosts"] = DEFAULT_RAG_CONFIG["opensearch"]["hosts"]
    if not isinstance(os_cfg.get("user"), str):
        os_cfg["user"] = DEFAULT_RAG_CONFIG["opensearch"]["user"]
    if not isinstance(os_cfg.get("password"), str):
        os_cfg["password"] = DEFAULT_RAG_CONFIG["opensearch"]["password"]
    if not isinstance(os_cfg.get("use_ssl"), bool):
        os_cfg["use_ssl"] = DEFAULT_RAG_CONFIG["opensearch"]["use_ssl"]
    if not isinstance(os_cfg.get("verify_certs"), bool):
        os_cfg["verify_certs"] = DEFAULT_RAG_CONFIG["opensearch"]["verify_certs"]

    for key, default_key in [
        ("kv", "kv"),
        ("vector", "vector"),
        ("graph", "graph"),
        ("doc_status", "doc_status"),
    ]:
        if not isinstance(storages.get(key), str) or not storages[key].strip():
            storages[key] = DEFAULT_RAG_CONFIG["storages"][default_key]

    if not isinstance(query_cfg.get("mode"), str) or not query_cfg["mode"].strip():
        query_cfg["mode"] = DEFAULT_RAG_CONFIG["query"]["mode"]
    if not isinstance(query_cfg.get("response_type"), str) or not query_cfg["response_type"].strip():
        query_cfg["response_type"] = DEFAULT_RAG_CONFIG["query"]["response_type"]
    if not isinstance(query_cfg.get("top_k"), int) or query_cfg["top_k"] <= 0:
        query_cfg["top_k"] = DEFAULT_RAG_CONFIG["query"]["top_k"]
    if not isinstance(query_cfg.get("enable_rerank"), bool):
        query_cfg["enable_rerank"] = DEFAULT_RAG_CONFIG["query"]["enable_rerank"]
    if not isinstance(query_cfg.get("only_need_context"), bool):
        query_cfg["only_need_context"] = DEFAULT_RAG_CONFIG["query"]["only_need_context"]
    if not isinstance(query_cfg.get("only_need_prompt"), bool):
        query_cfg["only_need_prompt"] = DEFAULT_RAG_CONFIG["query"]["only_need_prompt"]

    cfg["llm"] = llm_cfg
    cfg["embedding"] = emb_cfg
    cfg["opensearch"] = os_cfg
    cfg["storages"] = storages
    cfg["query"] = query_cfg
    return cfg


def _env_set_if_missing(name: str, value: Any) -> None:
    if value is None:
        return
    if os.environ.get(name):
        return
    os.environ[name] = str(value)


def apply_lightrag_env(config: dict[str, Any]) -> None:
    os_cfg = config.get("opensearch") or {}
    llm_cfg = config.get("llm") or {}
    emb_cfg = config.get("embedding") or {}

    # OpenSearch
    _env_set_if_missing("OPENSEARCH_HOSTS", os_cfg.get("hosts"))
    _env_set_if_missing("OPENSEARCH_USER", os_cfg.get("user"))
    _env_set_if_missing("OPENSEARCH_PASSWORD", os_cfg.get("password"))
    _env_set_if_missing("OPENSEARCH_USE_SSL", str(os_cfg.get("use_ssl", True)).lower())
    _env_set_if_missing("OPENSEARCH_VERIFY_CERTS", str(os_cfg.get("verify_certs", False)).lower())

    optional_os_keys = {
        "timeout": "OPENSEARCH_TIMEOUT",
        "max_retries": "OPENSEARCH_MAX_RETRIES",
        "number_of_shards": "OPENSEARCH_NUMBER_OF_SHARDS",
        "number_of_replicas": "OPENSEARCH_NUMBER_OF_REPLICAS",
        "knn_ef_construction": "OPENSEARCH_KNN_EF_CONSTRUCTION",
        "knn_m": "OPENSEARCH_KNN_M",
        "knn_ef_search": "OPENSEARCH_KNN_EF_SEARCH",
        "use_ppl_graphlookup": "OPENSEARCH_USE_PPL_GRAPHLOOKUP",
    }
    for cfg_key, env_key in optional_os_keys.items():
        if cfg_key in os_cfg:
            val = os_cfg.get(cfg_key)
            if isinstance(val, bool):
                val = str(val).lower()
            _env_set_if_missing(env_key, val)

    # LLM / Embedding bindings for LightRAG-Server/Core
    _env_set_if_missing("LLM_BINDING", llm_cfg.get("binding", "ollama"))
    _env_set_if_missing("LLM_BINDING_HOST", llm_cfg.get("host"))
    _env_set_if_missing("LLM_MODEL", llm_cfg.get("model"))

    _env_set_if_missing("EMBEDDING_BINDING", emb_cfg.get("binding", "ollama"))
    _env_set_if_missing("EMBEDDING_BINDING_HOST", emb_cfg.get("host"))
    _env_set_if_missing("EMBEDDING_MODEL", emb_cfg.get("model"))
    _env_set_if_missing("EMBEDDING_DIM", emb_cfg.get("dim"))


def make_embedding_func(config: dict[str, Any]):
    emb_cfg = config["embedding"]
    host = emb_cfg["host"]
    model = emb_cfg["model"]
    dim = emb_cfg["dim"]
    max_token_size = emb_cfg["max_token_size"]

    @wrap_embedding_func_with_attrs(
        embedding_dim=dim,
        max_token_size=max_token_size,
        model_name=model,
    )
    async def embedding_func(texts: list[str]):
        return await ollama_embed.func(
            texts,
            embed_model=model,
            host=host,
        )

    return embedding_func


def make_rag(config: dict[str, Any]) -> LightRAG:
    apply_lightrag_env(config)

    llm_cfg = config["llm"]
    storages = config["storages"]

    rag = LightRAG(
        working_dir=config["working_dir"],
        workspace=config["workspace"],
        llm_model_func=ollama_model_complete,
        llm_model_name=llm_cfg["model"],
        llm_model_kwargs={
            "host": llm_cfg["host"],
            "options": deepcopy(llm_cfg.get("options") or {}),
        },
        embedding_func=make_embedding_func(config),
        kv_storage=storages["kv"],
        vector_storage=storages["vector"],
        graph_storage=storages["graph"],
        doc_status_storage=storages["doc_status"],
    )
    return rag


async def _create_initialized_rag(config_path: Path | str | None = None, **overrides) -> LightRAG:
    base_cfg = get_rag_config(config_path)
    merged_cfg = build_rag_config(_deep_merge(base_cfg, overrides))
    rag = make_rag(merged_cfg)
    await rag.initialize_storages()
    return rag


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        raise RuntimeError(
            "Event already running, use "
            "async (aget_rag, ainsert_document, aquery_rag)."
        )
    return asyncio.run(coro)


async def aget_rag(config_path: Path | str | None = None, **overrides) -> LightRAG:
    return await _create_initialized_rag(config_path=config_path, **overrides)


def get_rag(config_path: Path | str | None = None, **overrides) -> LightRAG:
    return _run_async(_create_initialized_rag(config_path=config_path, **overrides))


async def ainsert_document(
    text: str,
    config_path: Path | str | None = None,
    **overrides,
) -> dict[str, Any]:
    if not isinstance(text, str):
        raise TypeError("text must be string.")
    text = text.strip()
    if not text:
        return {"status": "empty"}

    rag = await _create_initialized_rag(config_path=config_path, **overrides)
    try:
        await rag.ainsert(text)
        return {"status": "ok"}
    except Exception as e:
        logger.exception("LightRAG insert failed: %s", e)
        return {"status": "error", "message": str(e)}
    finally:
        try:
            await rag.finalize_storages()
        except Exception:
            logger.exception("LightRAG finalize after insert failed.")


def insert_document(
    text: str,
    config_path: Path | str | None = None,
    **overrides,
) -> dict[str, Any]:
    return _run_async(ainsert_document(text=text, config_path=config_path, **overrides))


async def aquery_rag(
    user_input: str,
    rag_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rag_kwargs = dict(rag_kwargs or {})
    config_path = rag_kwargs.pop("config_path", None)

    if not isinstance(user_input, str):
        raise TypeError("input must be string.")
    user_input = user_input.strip()
    if not user_input:
        return {"response": ""}

    base_cfg = get_rag_config(config_path)
    merged_cfg = build_rag_config(_deep_merge(base_cfg, rag_kwargs))
    rag = make_rag(merged_cfg)

    query_cfg = merged_cfg["query"]

    try:
        await rag.initialize_storages()

        result = await rag.aquery(
            user_input,
            param=QueryParam(
                mode=query_cfg["mode"],
                response_type=query_cfg["response_type"],
                top_k=query_cfg["top_k"],
                only_need_context=query_cfg["only_need_context"],
                only_need_prompt=query_cfg["only_need_prompt"],
                enable_rerank=query_cfg["enable_rerank"],
            ),
        )

        if isinstance(result, str):
            return {"response": result}

        if isinstance(result, dict):
            return result

        return {"response": str(result)}

    except Exception as e:
        logger.exception("LightRAG query failed: %s", e)
        return {"response": "", "error": str(e)}
    finally:
        try:
            await rag.finalize_storages()
        except Exception:
            logger.exception("LightRAG finalize after query failed.")


def query_rag(user_input: str, rag_kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    return _run_async(aquery_rag(user_input=user_input, rag_kwargs=rag_kwargs))