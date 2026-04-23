
from ..helpers import ensure_import, resolve_config_path, plugin_logger
from zotero_rdf_server.utils import load_dict_like
from typing import Dict, Iterator, Tuple, Any, Iterable, Callable
from functools import lru_cache
from pathlib import Path
import json
from copy import deepcopy
ensure_import("ollama==0.6.1", requirements=None)
from ollama import Client

logger=plugin_logger()

@lru_cache(maxsize=8)
def get_llm_config(config_path: Path | str | None = None) -> dict[str, Any]:
    if isinstance(config_path, Path):
        config_path = str(config_path)
    cfg_path = resolve_config_path(config_path)
    cfg = load_dict_like(cfg_path, label="Ollama Config", verbose=False)
    return cfg.get("llm") or cfg
    
def make_llm_client(config:dict): 
    client_cfg = config.get('client')
    logger.debug(f"{client_cfg}")
    ollama_cfg = client_cfg or {'host':'http://ollama:11434'}
    return Client(**ollama_cfg)

DEFAULT_CHAT = {
    "model": "qwen2.5:7b",
    "messages": [
        {"role": "system", "content": "Definition"},
    ],
}


def build_chat_config(llmcfg: dict[str, Any] | None) -> dict[str, Any]:
    task = llmcfg.pop('tasks', 'chat')
    # TODO better logic
    cfg = llmcfg.get('chats') or {}
    cfg = deepcopy((llmcfg or {}).get(task, DEFAULT_CHAT))

    if not isinstance(cfg, dict):
        raise ValueError("llmcfg['chat'] must be Dictionary.")

    model = cfg.get("model")
    if not isinstance(model, str) or not model.strip():
        cfg["model"] = DEFAULT_CHAT["model"]

    messages = cfg.get("messages")
    if not isinstance(messages, list):
        cfg["messages"] = deepcopy(DEFAULT_CHAT["messages"])
    else:
        cleaned_messages = []
        for msg in messages:
            if (
                isinstance(msg, dict)
                and isinstance(msg.get("role"), str)
                and isinstance(msg.get("content"), str)
            ):
                cleaned_messages.append(
                    {"role": msg["role"], "content": msg["content"]}
                )

        if not cleaned_messages:
            cleaned_messages = deepcopy(DEFAULT_CHAT["messages"])

        cfg["messages"] = cleaned_messages

    return cfg


def llm(user_input: str, llm_kwargs: dict | None = None) -> str:
    import requests
    llm_kwargs = dict(llm_kwargs or {})
    config_path = llm_kwargs.pop('config_path')
    if not isinstance(user_input, str):
        raise TypeError("input must be string.")
    llm_config = get_llm_config(config_path)
    client = make_llm_client(llm_config)

    llmcfg = llm_kwargs | llm_config
    CHAT = build_chat_config(llmcfg)
    host = llmcfg.get("client", {}).get('host')
    logger.info(f"Using Ollama host: {host}")
    user_input = user_input.strip()
    if not user_input:
        return ""
    
    payload = {
        "model": CHAT["model"],
        "messages": deepcopy(CHAT["messages"]) + [
            {"role": "user", "content": user_input}
        ],
    }

    logger.debug(json.dumps(payload, indent=4))

    try:
        r = requests.get(f"{host}/api/tags", timeout=2)
        r.raise_for_status()
        logger.debug("Ollama reachable.")
    except Exception as e:
        logger.error(f"Ollama not reachable at {host}: {e}")
    
    try:
        response = client.chat(**payload)
        content = getattr(response.message, "content", None)
        if not isinstance(content, str):
            raise ValueError("Response invalid.")
        # content_dict = load_dict_like(content.strip(),default={'response':''})
        return content.strip() # content_dict

    except Exception as e:
        logger.exception("Ollama request failed: %s", e)
        return '' # {'response': ''}