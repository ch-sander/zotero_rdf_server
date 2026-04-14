from .helpers import ensure_import
ensure_import("ollama==0.6.1", requirements=None)
from ollama import Client

from .db import make_client, resolve_config_path, get_os_config
from .helpers import plugin_logger
logger=plugin_logger()

cfg_path = resolve_config_path()
logger.debug(f"Loading config from {cfg_path}")
llmcfg = get_os_config(cfg_path)
logger.debug(f"{llmcfg}")
client = Client(llmcfg)
CHAT = llmcfg.get("chat", {'model':'qwen2.5:7b', 'messages':[
            {'role': 'system', 'content': 'Definition'},
            {'role': 'user', 'content': 'Command'},
        ]})

def llm():
    response = client.chat(CHAT)
    return response.message.content