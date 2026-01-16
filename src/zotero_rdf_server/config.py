import yaml, os, requests, json
from pathlib import Path
from zotero_rdf_server.logging_config import logger, setup_logging
from urllib.parse import urlparse
import sys


setup_logging("INFO")
try:
    WORKDIR = Path(os.getenv("WORKDIR", Path())).resolve()
    logger.info(f"WORKDIR set to {WORKDIR}")
    logger.info(f"Loading YAML...")
except Exception as e:
    logger.critical(f"Failed to set WORKDIR!")

def load_config(source):
    parsed = urlparse(str(source))

    if parsed.scheme in ('http', 'https'):
        response = requests.get(source)
        response.raise_for_status()
        content = response.text
        suffix = Path(parsed.path).suffix.lower()
    else:
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(f"Configuration file not found: {source}")
        content = source_path.read_text(encoding="utf-8")
        suffix = source_path.suffix.lower()

    if suffix in ('.yaml', '.yml'):
        config = yaml.safe_load(content)
    elif suffix == '.json':
        config = json.loads(content)
    else:
        raise ValueError(f"Unsupported config file format: {suffix}")

    logger.info(f"Configuration loaded from {source}")
    return config

def safe_path(path_str: str | Path | None, base_dir: Path | str = WORKDIR) -> Path:
    if path_str:
        p = Path(path_str) # if not isinstance(path_str, Path) else Path(path_str)
        if base_dir:
            base_dir = str(base_dir) if not isinstance(base_dir, Path) else Path(base_dir)
        else:
            base_dir = Path().resolve()
            
        return p if p.is_absolute() else (base_dir / p).resolve()
    else:
        return None

config_path = safe_path(os.getenv("CONFIG_FILE", "config.yaml"))
zotero_config_path = safe_path(os.getenv("ZOTERO_CONFIG_FILE", "zotero.yaml"))

try:
    config =  load_config(config_path)
except Exception as e:
    logger.critical(f"Failed to load {config_path}: {e}!")
    logger.critical(f"EXITING")
    sys.exit(1)
    

try:
    zotero_config = load_config(zotero_config_path)
except Exception as e:
    logger.critical(f"Failed to load {zotero_config_path}: {e}!")
    logger.critical(f"EXITING")
    sys.exit(1)

config = config or {}
zotero_config = zotero_config or {}

log_level = config["server"].get("log_level", "info").upper()

setup_logging(log_level)

# --- Config ---
REFRESH_INTERVAL = config["server"].get("refresh_interval", 0)
DELAY = config["server"].get("delay", 60)
STORE_MODE = "directory"
STORE_DIRECTORY = safe_path(config["server"].get("store_directory", "/app/data"))
EXPORT_DIRECTORY = safe_path(config["server"].get("export_directory", "/app/exports"))
IMPORT_DIRECTORY = safe_path(config["server"].get("import_directory", "/app/import"))
BACKUP_DIRECTORY = safe_path(config["server"].get("backup_directory", "/app/backup"))

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
MAX_DATA = zotero_config.get("max_data", 0)
try:
    MAX_DATA = int(MAX_DATA)
except:
    MAX_DATA = int(0)

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

# RDF Constants
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
XSD_NS = "http://www.w3.org/2001/XMLSchema#"
SKOS_ALT = "http://www.w3.org/2004/02/skos/core#altLabel"
SKOS_PREF = "http://www.w3.org/2004/02/skos/core#prefLabel"
SKOS_BROADER = "http://www.w3.org/2004/02/skos/core#broader"
SKOS_CONCEPT = "http://www.w3.org/2004/02/skos/core#Concept"
PROV_TIMESTAMP = "http://www.w3.org/ns/prov#generatedAtTime"
OWL_SAME_AS = "http://www.w3.org/2002/07/owl#sameAs"
PREFIXES = {"zot":ZOT_NS, "rdfs":"http://www.w3.org/2000/01/rdf-schema#", "owl":"http://www.w3.org/2002/07/owl#", "rdf":"http://www.w3.org/1999/02/22-rdf-syntax-ns#", "xsd":XSD_NS, "skos":"http://www.w3.org/2004/02/skos/core#", "prov":"http://www.w3.org/ns/prov#"}
REGEX_PATTERN = f"{ZOT_NS}regex"

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