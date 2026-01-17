import requests, json, time
from requests.adapters import HTTPAdapter, Retry
from requests.exceptions import ReadTimeout, RequestException

from .logging_config import logger, setup_logging
from .config import *
from .utils import *

class ZoteroLibrary:
    def __init__(self, config: dict, check: bool = True):
        self.name = config.get("name", "no name")
        self.load_mode = config.get("load_mode", "json")
        self.library_type = config.get("library_type", None)
        if self.library_type == "group": self.library_type == "groups"
        self.library_id = config.get("library_id", None)
        self.api_key = config.get("api_key", None)
        self.user = config.get("user", ZOT_API_USER)
        self.rdf_export_format = config.get("rdf_export_format", "rdf_zotero")
        self.api_query_params = config.get("api_query_params") or {}
        self.base_api_url = f"{ZOT_API_URL}{self.library_type}/{self.library_id}".strip("#/")
        self.base_url = str(config.get("base_uri", f"{ZOT_BASE_URL}{self.library_type}/{self.library_id}")).strip("/#")
        self.knowledge_base_graph = str(config.get("knowledge_base_graph", self.base_url)).strip("/#")
        
        self.load_from = safe_path(str(config.get("load_from",IMPORT_DIRECTORY / self.name)).replace("$",str(self.library_id)))

        self.save_to = config.get("save_to")
        if self.save_to:
            self.save_to = safe_path(str(self.save_to).replace("$",str(self.library_id)))

        self.headers = {"Zotero-API-Key": self.api_key,
                        "Zotero-API-Version": "3",
                        "Accept": "application/json",
                        "User-Agent": self.user}
        self.map = config.get("map") or {}
        self.sync = {}

        if (
            config.get("sync") and
            config["sync"].get('library_type') and
            config["sync"].get('library_id') and
            config["sync"].get('api_key')
        ):
            self.sync = config["sync"]
            self.sync['base_uri'] = f"{ZOT_BASE_URL}{self.sync['library_type']}/{self.sync['library_id']}"
        
        # PLUG-IN Config
        self.plugin = config.get("plugin") or {}
        self.parser = config.get("notes_parser") or {}
        self.taxonomy = {}
        if config.get("taxonomy"):
            self.taxonomy = config["taxonomy"]
            self.taxonomy['note_key'] = config.get('note_key', None)

        # check settings
        if check:
            passing = True
            if not any([str(self.base_url).startswith("http"),str(self.base_api_url).startswith("http"),str(self.knowledge_base_graph).startswith("http")]):
                passing = False
                logger.warning(f"{self.name}: Some library config variable is expected to be a IRI/URI but is not!")
            if not str(self.library_id).isdigit() and not self.library_type == "knowledge base":
                passing = False
                logger.error(f"{self.name}: Invalid library ID --> {type(self.library_id)}!")
            if not self.load_mode in ["json", "rdf", "manual_import"]:
                passing = False
                logger.warning(f"{self.name}: Invalid load_mode {self.load_mode}!")
            if not self.library_type in ["groups", "user", "knowledge base"]:            
                passing = False
                logger.error(f"{self.name}: Invalid library_type {self.library_type}!")
            if not self.rdf_export_format in ["rdf_zotero", "rdf_bibliontology"] and self.load_mode == "rdf":
                passing = False
                logger.warning(f"{self.name}: rdf_export_format {self.rdf_export_format} has not been tested!")
            if any([(self.name and not isinstance(self.name,str)),(self.api_key and not isinstance(self.api_key,str)),(self.map and not isinstance(self.map,dict)),(self.api_query_params and not isinstance(self.api_query_params,dict)),(self.map.get("white") and not isinstance(self.map["white"],list))]):
                passing = False
                logger.warning(f"{self.name}: Invalid optional argument!")

            if not passing:
                logger.error(f"####################################################")
                logger.error(f"####################################################")
                logger.error(f"####################################################")
                logger.error(f"{self.name}: Problematic library config, check warnings!")
                logger.error(f"####################################################")
                logger.error(f"####################################################")
                logger.error(f"####################################################")
            else:
                logger.info(f"{self.name}: Valid library config!") 

    def fetch_paginated(self, endpoint: str, max_data: int = MAX_DATA) -> list:
        results = []
        start = 0
        logger.info("Initialize session")

        retries = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        adapter = HTTPAdapter(max_retries=retries)
        filter_collection = self.api_query_params.pop("collection", None)
        if endpoint == "items" and filter_collection and isinstance(filter_collection, str):
            endpoint = f"collections/{filter_collection}/items"

        with requests.Session() as session:
            session.mount("https://", adapter)
            session.mount("http://", adapter)

            while True:
                params = {
                    "format": "json",
                    "limit": LIMIT,
                    "start": start,
                    **self.api_query_params
                }
                req = requests.Request(
                    method="GET",
                    url=f"{self.base_api_url}/{endpoint}",
                    headers=self.headers,
                    params=params
                )
                prepared = req.prepare()
                logger.info(f"Sending API request: {prepared.method} {prepared.url}")
                for k, v in prepared.headers.items():
                    logger.debug(f"Header: {k}: {v}")

                try:
                    response = session.send(prepared, timeout=(5, 30))
                    response.raise_for_status()
                    data = response.json()
                except ReadTimeout:
                    logger.error(f"Timeout after 30s at {prepared.url}")
                    raise
                except RequestException as e:
                    logger.error(f"Request error: {e}")
                    raise

                if not data:
                    logger.info(f"No more data (start={start})")
                    break

                results.extend(data)
                logger.info(f"Fetched {len(data)} items (start={start})")
                start += LIMIT                
                if max_data and max_data > 0 and start >= max_data:
                    logger.warning(f"Aborting pagination: max of {max_data} items reached.")
                    break

                time.sleep(1)

        return results

    def fetch_items(self, json_path:str | Path = None) -> list:
        if self.load_mode == "manual_import":
            json_path = safe_path(json_path)
            if not json_path or not json_path.is_file():
                raise FileNotFoundError(f"JSON path not found: {json_path}")

            with open(json_path, "r", encoding="utf-8") as f:
                items = json.load(f)

            if not isinstance(items, list):
                raise ValueError(f"Expected list of items in JSON file, got {type(items).__name__}")

            return items
        elif self.load_mode == "json":
            return self.fetch_paginated("items")
        else:
            return None

    def fetch_collections(self, json_path:str | Path = None) -> list:
        if self.load_mode == "manual_import":
            json_path = safe_path(json_path)
            if not json_path or not json_path.is_file():
                raise FileNotFoundError(f"JSON path not found: {json_path}")

            with open(json_path, "r", encoding="utf-8") as f:
                cols = json.load(f)

            if not isinstance(cols, list):
                raise ValueError(f"Expected list of collections in JSON file, got {type(cols).__name__}")

            return cols
        if self.load_mode == "json":
            return self.fetch_paginated("collections")
        else:
            return None

    def fetch_rdf_export(self) -> bytes:
        params = {"format": self.rdf_export_format, "limit": LIMIT, **self.api_query_params}
        response = requests.get(f"{self.base_api_url}/items", headers=self.headers, params=params)
        response.raise_for_status()
        return response.content  # RDF XML as Bytes