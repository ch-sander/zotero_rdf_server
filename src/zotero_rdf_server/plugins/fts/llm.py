
from .helpers import ensure_import, resolve_config_path, plugin_logger
from typing import Dict, Iterator, Tuple, Any, Iterable, Callable
from functools import lru_cache
from pathlib import Path
from copy import deepcopy
from .helpers import ensure_import
ensure_import("ollama==0.6.1", requirements=None)
from ollama import Client

logger=plugin_logger()

@lru_cache(maxsize=8)
def get_llm_config(config_path: Path) -> dict[str, Any]:
    from zotero_rdf_server.utils import load_dict_like
    cfg = load_dict_like(config_path,label="Ollama Config",verbose=False)
    return cfg.get("llm") or {'host':'http://ollama:11434'}


cfg_path = resolve_config_path()
logger.debug(f"Loading config from {cfg_path}")
llmcfg = get_llm_config(cfg_path)
logger.debug(f"{llmcfg}")
client = Client(**llmcfg)

DEFAULT_CHAT = {
    "model": "qwen2.5:7b",
    "messages": [
        {"role": "system", "content": "Definition"},
    ],
}


def build_chat_config(llmcfg: dict[str, Any] | None) -> dict[str, Any]:
    cfg = deepcopy((llmcfg or {}).get("chat", DEFAULT_CHAT))

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


CHAT = build_chat_config(llmcfg)


def llm(user_input: str) -> str:
    if not isinstance(user_input, str):
        raise TypeError("input muss ein String sein.")

    user_input = user_input.strip()
    if not user_input:
        return ""

    payload = {
        "model": CHAT["model"],
        "messages": deepcopy(CHAT["messages"]) + [
            {"role": "user", "content": user_input}
        ],
    }

    try:
        response = client.chat(**payload)

        content = getattr(response.message, "content", None)
        if not isinstance(content, str):
            raise ValueError("Response invalid.")

        return content.strip()

    except Exception as e:
        logger.exception("Ollama request failed: %s", e)
        return ""