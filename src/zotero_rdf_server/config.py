import yaml, os, requests, json
from pathlib import Path
from .logging_config import logger, setup_logging
from urllib.parse import urlparse
import sys

# RDF Constants
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
XSD_NS = "http://www.w3.org/2001/XMLSchema#"
SKOS_PREF = "http://www.w3.org/2004/02/skos/core#prefLabel"
SKOS_BROADER = "http://www.w3.org/2004/02/skos/core#broader"
SKOS_CONCEPT = "http://www.w3.org/2004/02/skos/core#Concept"
PROV_TIMESTAMP = "http://www.w3.org/ns/prov#generatedAtTime"
OWL_SAME_AS = "http://www.w3.org/2002/07/owl#sameAs"
PURL_RELATED = "http://purl.org/dc/elements/1.1/relation"

MAPPING_BASE = "https://zotero-rdf-server.org/mapping/"
MAP_ENTRY_TYPE = f"{MAPPING_BASE}Entry"
MAP_TARGET     = f"{MAPPING_BASE}target"     # Entry -> Entity IRI
MAP_LABEL      = f"{MAPPING_BASE}label"      # Entry -> Literal (Variante)
MAP_REGEX      = f"{MAPPING_BASE}pattern"    # Entry -> Literal (Regex Pattern) optional
MAP_TYPE_HINT  = f"{MAPPING_BASE}typeHint"   # Entry -> Literal or IRI
MAP_UPDATED_AT = f"{MAPPING_BASE}updatedAt"  # optional


# Additional Constants
FUZZY = 90
LANG_MAP = {
                "de": ["deutsch", "german", "allemand", "alemán", "tedesco", "deu", "ger", "de"],
                "en": ["englisch", "english", "anglais", "inglés", "inglese", "eng", "en"],
                "fr": ["französisch", "french", "français", "francese", "fre", "fra", "fr"],
                "it": ["italienisch", "italian", "italien", "italiano", "ita", "it"],
                "es": ["spanisch", "spanish", "español", "espanol", "esp", "spa", "es"],
                "la": ["latein", "latin", "latino", "lat", "la"],
                "pt": ["portugiesisch", "portuguese", "português", "por", "pt"],
                "ru": ["russisch", "russian", "русский", "rus", "ru"],
                "ja": ["japanisch", "japanese", "日本語", "jpn", "ja"],
                "zh": ["chinesisch", "chinese", "中文", "漢語", "汉语", "chi", "zho", "zh"],
                "ar": ["arabisch", "arabic", "العربية", "ara", "ar"],
                "default": "und" # used if none found
            }

APP_USER = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Zotero_RDF_server/1.0 (+https://github.com/ch-sander/zotero_rdf_server)"}

from dotenv import load_dotenv
load_dotenv()

setup_logging("INFO")
try:
    WORKDIR = Path(os.getenv("WORKDIR") or Path.cwd().parent).resolve()
    logger.info(f"WORKDIR in ENV: {os.getenv('WORKDIR')}")
    logger.info(f"WORKDIR set to {WORKDIR}")    
except Exception as e:
    logger.critical(f"Failed to set WORKDIR!")

def load_config(source):
    from .utils import load_dict_like    
    from string import Template
    config =  load_dict_like(source, label="Loading initial config")
    logger.debug(json.dumps(config,indent=4))
    if config.get("inject_env"):
        try:
            logger.warning(f"Trying to inject .env into {source}")            
            logger.debug(os.environ)
            config_str = json.dumps(config)
            config_str = Template(config_str).safe_substitute(os.environ)
            config = json.loads(config_str)
        except Exception as e:
            logger.error(f"Could not inject .env into {source}: {e}")
    return config

def safe_path(path_str: str | Path | None, base_dir: Path | str = WORKDIR, create: bool = True) -> Path | None:
    if path_str:
        p = Path(path_str)
        
        base_dir = Path(base_dir) if base_dir else Path.cwd().parent # Path().resolve()
        result = p if p.is_absolute() else (base_dir / p).resolve()
        
        if create:
            result.mkdir(parents=True, exist_ok=True)
        
        return result
    logger.warning(f"Path not valid: {path_str} in {base_dir}")
    return None

def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

config_path = os.getenv("CONFIG_FILE", "config.yaml")
zotero_config_path = os.getenv("ZOTERO_CONFIG_FILE", "zotero.yaml")

try:    
    logger.info(f"Loading config YAML...")
    config =  load_config(config_path)
except Exception as e:
    config = None
    logger.warning(f"Failed to load {config_path}: {e}!")
    # logger.critical(f"EXITING")
    # sys.exit(1)
    

try:
    logger.info(f"Loading Zotero YAML...")
    zotero_config = load_config(zotero_config_path)
except Exception as e:
    zotero_config = None
    logger.warning(f"Failed to load {zotero_config_path}: {e}!")
    # logger.critical(f"EXITING")
    # sys.exit(1)

config = config or {}
zotero_config = zotero_config or {}

# --- Config ---
server_cfg = config.get("server") or {}

log_level = (
    os.getenv("LOG_LEVEL")
    or server_cfg.get("log_level")
    or "info"
).strip().upper()

setup_logging(log_level)
logger.info(f"Log level set to {log_level}")

REFRESH_INTERVAL = int(
    os.getenv("REFRESH_INTERVAL")
    or server_cfg.get("refresh_interval")
    or 0
)

DELAY = int(
    os.getenv("DELAY")
    or server_cfg.get("delay")
    or 60
)

EXPORT_DIRECTORY = safe_path(
    os.getenv("EXPORT_DIRECTORY")
    or server_cfg.get("export_directory")
    or "/app/exports"
)

STATIC_UI_DIRECTORY = safe_path(
    os.getenv("STATIC_UI_DIRECTORY")
    or server_cfg.get("static_ui_directory")
    or "/app/ui"
)

STATIC_UI_PREFIX = (
    os.getenv("STATIC_UI_PREFIX")
    or server_cfg.get("static_ui_prefix", "/ui")
    )

API_UI_URL = (
    os.getenv("API_UI_URL")
    or server_cfg.get("api_ui_url", "/")
    )

FASTAPI_META = server_cfg.get("fastapi", {})

FASTAPI_APP_NAME = os.getenv("FASTAPI_APP_NAME")

if FASTAPI_APP_NAME:
    FASTAPI_META['title'] = FASTAPI_APP_NAME

STATIC_UI_PREFIX = f"/{STATIC_UI_PREFIX.lstrip('/').rstrip('/')}" if STATIC_UI_PREFIX else None

INCLUDE_CLOSED_ROUTER = env_bool(
    "INCLUDE_CLOSED_ROUTER",
    server_cfg.get("include_closed_router", True),
)

INCLUDE_OPEN_ROUTER = env_bool(
    "INCLUDE_OPEN_ROUTER",
    server_cfg.get("include_open_router", True),
)

INCLUDE_PLUGINS = env_bool(
    "INCLUDE_PLUGINS",
    server_cfg.get("include_plugins", True),
)

IMPORT_DIRECTORY = safe_path(
    os.getenv("IMPORT_DIRECTORY")
    or server_cfg.get("import_directory")
    or "/app/import"
)

BACKUP_DIRECTORY = safe_path(
    os.getenv("BACKUP_DIRECTORY")
    or server_cfg.get("backup_directory")
    or "/app/backup"
)

STORE_MODE = os.getenv(
    "STORE_MODE",
    server_cfg.get("store_mode", "directory_rw")
).strip().lower()

ROOT_PATH = os.getenv(
    "ROOT_PATH",
    server_cfg.get("root_path", "")
)

ROOT_PATH = f"/{ROOT_PATH.lstrip('/').rstrip('/')}" if ROOT_PATH else None

if STORE_MODE not in {"memory", "directory_rw", "directory_ro"}:
    STORE_MODE = "directory_rw"

STORE_DIRECTORY = safe_path(
    os.getenv("STORE_DIRECTORY")
    or server_cfg.get("store_directory")
    or "/app/data"
)

API_USER = (
    os.getenv("API_USER")
    or server_cfg.get("api_user")
    or None
)

API_PASSWORD = (
    os.getenv("API_PASSWORD")
    or server_cfg.get("api_password")
    or None
)

REFRESH = REFRESH_INTERVAL >= 0

if REFRESH_INTERVAL >= 30:
    logger.info(f"Refresh set to {REFRESH_INTERVAL} seconds")
elif REFRESH_INTERVAL == -1:
    logger.info("Refresh deactivated")
elif REFRESH_INTERVAL == 0:
    logger.info("Refresh only at startup")
else:
    logger.info("Refresh interval incorrect and refresh disabled! A minimum of 30 seconds is required!")

def set_defaults(lib_cfg: dict, master_cfg: dict, mode: str = "default", merge_keys: list = None) -> dict:
    merged = lib_cfg.copy()
    for key, value in master_cfg.items():
        if key not in merged:
            merged[key] = value
        elif mode == "override":
            merged[key] = value
        elif mode == "merge":
            if merge_keys and key in merge_keys and isinstance(value, dict) and isinstance(merged[key], dict):
                merged[key] = set_defaults(merged[key], value, mode="merge", merge_keys=merge_keys)
    return merged

# --- Zotero Config ----

ZOTERO_DEFAULT_CONFIGS = zotero_config.get("defaults", {})
ZOTERO_DEFAULT_MODE = ZOTERO_DEFAULT_CONFIGS.get("mode", "default")
ZOTERO_CONFIGS = zotero_config.get("context", {})
ZOTERO_KB_CONFIG = [] # TODO not yet in use
ZOTERO_LIBRARIES_CONFIGS = []
LIMIT = zotero_config.get("limit", 100)
# MAX_DATA = zotero_config.get("max_data", 0)
# try:
#     MAX_DATA = int(MAX_DATA)
# except:
#     MAX_DATA = int(0)

for lib_cfg in zotero_config.get("libraries", []):
    merged_cfg = set_defaults(lib_cfg, ZOTERO_DEFAULT_CONFIGS, ZOTERO_DEFAULT_MODE)
    if merged_cfg.get("library_type") == "knowledge base":
        ZOTERO_KB_CONFIG.append(merged_cfg)
    ZOTERO_LIBRARIES_CONFIGS.append(merged_cfg)

# Zotero Constants
ZOT_NS = ZOTERO_CONFIGS.get("vocab", "http://www.zotero.org/namespaces/export#")
ZOT_API_URL = ZOTERO_CONFIGS.get("api_url", "https://api.zotero.org/")
ZOT_API_USER = ZOTERO_CONFIGS.get("user", "Zotero RDF Server App")
ZOT_BASE_URL = ZOTERO_CONFIGS.get("base_url", "https://www.zotero.org/")
ZOT_SCHEMA = ZOTERO_CONFIGS.get("schema") # "https://api.zotero.org/schema"
REGEX_PATTERN = f"{ZOT_NS}regex"

PREFIXES = {"zot":ZOT_NS, "rdfs":"http://www.w3.org/2000/01/rdf-schema#", "owl":"http://www.w3.org/2002/07/owl#", "rdf":"http://www.w3.org/1999/02/22-rdf-syntax-ns#", "xsd":XSD_NS, "skos":"http://www.w3.org/2004/02/skos/core#", "prov":"http://www.w3.org/ns/prov#", "dc":"http://purl.org/dc/elements/1.1/", "schema":"https://schema.org/", "dct":"http://purl.org/dc/terms/", "zmap": MAPPING_BASE}