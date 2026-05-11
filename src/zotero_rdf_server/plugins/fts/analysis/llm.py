
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


def build_chat_config(llmcfg: dict[str, Any] | None, task: str = "chat") -> dict[str, Any]:
    # task = llmcfg.pop('task', 'chat')
    # chats = llmcfg.get("chats") or {}

    # if task not in chats:
    #     logger.warning(f"task {task} not found")
    #     chats[task] = chats
    
    # cfg = deepcopy(chats[task])
    chats = llmcfg.get("chats") or {}

    if task not in chats:
        raise ValueError(f"Chat task not found: {task}")
    cfg = deepcopy(chats[task])


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
def build_langextract_examples(examples_cfg: list[dict[str, Any]]):
    ensure_import("langextract", requirements=None)
    import langextract as lx

    return [
        lx.data.ExampleData(
            text=ex["text"],
            extractions=[
                lx.data.Extraction(
                    extraction_class=e["extraction_class"],
                    extraction_text=e["extraction_text"],
                    attributes=e.get("attributes", {}),
                )
                for e in ex.get("extractions", [])
            ],
        )
        for ex in examples_cfg
    ]


def run_langextract(
    user_input: str,
    llm_config: dict[str, Any],
    task: str,
    overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    ensure_import("langextract", requirements=None)
    import langextract as lx

    overrides = overrides or {}
    le_tasks = llm_config.get("langextract") or {}

    if task not in le_tasks:
        raise ValueError(f"LangExtract task not found: {task}")

    cfg = deepcopy(le_tasks[task])
    cfg.update({k: v for k, v in overrides.items() if v is not None})

    client_host = (llm_config.get("client") or {}).get("host", "http://ollama:11434")

    model = cfg.get("model", "qwen2.5:7b-instruct")
    model_url = cfg.get("model_url") or client_host

    examples = build_langextract_examples(cfg.get("examples", []))

    result = lx.extract(
        text_or_documents=user_input,
        prompt_description=cfg["prompt_description"],
        examples=examples,
        model_id=f"ollama:{model}",
        model_url=model_url,
        temperature=cfg.get("temperature", 0.0),
        max_char_buffer=cfg.get("max_char_buffer", 1200),
        extraction_passes=cfg.get("extraction_passes", 1),
        max_workers=cfg.get("max_workers", 1),
    )

    items = []

    for e in result.extractions:
        interval = getattr(e, "char_interval", None)

        if interval is None:
            continue

        start = interval.start_pos
        end = interval.end_pos

        items.append({
            "concept": e.extraction_text,
            "normalized": e.attributes.get("normalized"),
            "translation": e.attributes.get("translation"),
            "start": start,
            "end": end,
            "evidence": user_input[start:end],
        })

    return items

def llm(user_input: str, llm_kwargs: dict | None = None) -> Any:
    llm_kwargs = dict(llm_kwargs or {})
    config_path = llm_kwargs.pop("config_path")
    framework = llm_kwargs.pop("framework", "ollama")
    task = llm_kwargs.pop("task", "chat")

    config = get_llm_config(config_path)

    if framework == "langextract":
        return run_langextract(user_input, config, task, llm_kwargs)

    if framework == "ollama":
        return run_ollama_chat(user_input, config, task, llm_kwargs)

    raise ValueError(f"Unknown LLM framework: {framework}")

def run_ollama_chat_legacy(user_input: str, llm_kwargs: dict | None = None) -> str:
    import requests
    llm_kwargs = dict(llm_kwargs or {})
    config_path = llm_kwargs.pop('config_path')
    if not isinstance(user_input, str):
        raise TypeError("input must be string.")
    
    llm_config = get_llm_config(config_path)
    client = make_llm_client(llm_config)

    
    llmcfg = llm_kwargs | llm_config
    task = llmcfg.get('task', 'n/a')
    
    CHAT = build_chat_config(llmcfg)
    host = llmcfg.get("client", {}).get('host')
    
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
        logger.debug(f"Using Ollama host: {host}, model: {payload.get('model', 'n/a')}, task: {task}")
        response = client.chat(**payload)
        content = getattr(response.message, "content", None)
        if not isinstance(content, str):
            raise ValueError("Response invalid.")
        # content_dict = load_dict_like(content.strip(),default={'response':''})
        return content.strip() # content_dict

    except Exception as e:
        logger.exception("Ollama request failed: %s", e)
        return '' # {'response': ''}
    
def run_ollama_chat(
    user_input: str,
    llm_config: dict[str, Any],
    task: str,
    overrides: dict[str, Any] | None = None,
) -> str:
    overrides = overrides or {}

    client = make_llm_client(llm_config)
    chat_cfg = build_chat_config(llm_config, task=task)

    if "model" in overrides:
        chat_cfg["model"] = overrides["model"]

    payload = {
        "model": chat_cfg["model"],
        "messages": deepcopy(chat_cfg["messages"]) + [
            {"role": "user", "content": user_input.strip()}
        ],
        "options": {
            "temperature": chat_cfg.get("temperature", 0.1),
            "top_p": chat_cfg.get("top_p", 0.8),
            "num_predict": chat_cfg.get("max_tokens", 500),
        },
    }

    response = client.chat(**payload)
    content = getattr(response.message, "content", None)

    if not isinstance(content, str):
        raise ValueError("Invalid Ollama response.")

    return content.strip()