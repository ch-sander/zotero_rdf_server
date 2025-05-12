import yaml, os
from zotero_rdf_server.logging_config import logger, setup_logging

config_path = os.getenv("CONFIG_FILE", "config.yaml")
zotero_config_path = os.getenv("ZOTERO_CONFIG_FILE", "zotero.yaml")

with open(config_path, "r") as f:
    config = yaml.safe_load(f)

with open(zotero_config_path, "r") as f:
    zotero_config = yaml.safe_load(f)

config = config or {}
zotero_config = zotero_config or {}

log_level = config["server"].get("log_level", "info").upper()

setup_logging(log_level)

# --- Config ---
REFRESH_INTERVAL = config["server"].get("refresh_interval", 0)
DELAY = config["server"].get("delay", 60)
STORE_MODE = "directory"
STORE_DIRECTORY = config["server"].get("store_directory", "/app/data")
EXPORT_DIRECTORY = config["server"].get("export_directory", "/app/exports")
IMPORT_DIRECTORY = config["server"].get("import_directory", "/app/import")
BACKUP_DIRECTORY = config["server"].get("backup_directory", "/app/backup")

LIMIT = 100

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

ZOTERO_LIBRARIES_CONFIGS = [
    set_defaults(lib_cfg, ZOTERO_DEFAULT_CONFIGS, ZOTERO_DEFAULT_MODE)
    for lib_cfg in zotero_config.get("libraries", [])
]

# --- Constants ---
ZOT_NS = ZOTERO_CONFIGS.get("vocab", "http://www.zotero.org/namespaces/export#")
ZOT_API_URL = ZOTERO_CONFIGS.get("api_url", "https://api.zotero.org/")
ZOT_BASE_URL = ZOTERO_CONFIGS.get("base_url", "https://www.zotero.org/")
ZOT_SCHEMA = ZOTERO_CONFIGS.get("schema") # "https://api.zotero.org/schema"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
XSD_NS = "http://www.w3.org/2001/XMLSchema#"
SKOS_ALT = "http://www.w3.org/2004/02/skos/core#altLabel"
PREFIXES = {"zot":ZOT_NS, "rdfs":"http://www.w3.org/2000/01/rdf-schema#", "owl":"http://www.w3.org/2002/07/owl#", "rdf":"http://www.w3.org/1999/02/22-rdf-syntax-ns#", "xsd":XSD_NS, "skos":"http://www.w3.org/2004/02/skos/core#"}

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