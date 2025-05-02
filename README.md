# Zotero RDF SPARQL Server (Pyoxigraph-only)

This server loads multiple Zotero libraries into an RDF graph,
exposes a local SPARQL endpoint, and allows exporting the graph.

## Features
- Load mode: JSON (Zotero API), RDF (Zotero API), or manual RDF import
- Only Pyoxigraph in bulk-load where possible
- Configurable API Query parameters (e.g., itemType, tag, collection)
- Correct Zotero export namespace
- FastAPI SPARQL endpoint not yet deployed
- Export as TriG or N-Quads
- Docker and Compose ready
- includes Oxigraph server in docker to query at port 7878

## Configuration

Place both YAML filenames in your `.env`, not in the code or Dockerfile. Only these two environment variables need updating when you rename or move configuration files:

```bash
CONFIG_FILE=custom-config.yaml
ZOTERO_CONFIG_FILE=custom-zotero.yaml
```

Docker-Compose will mount these files into `/app` and your Python code loads them via `os.getenv(...)` with sensible defaults (`config.yaml` and `zotero.yaml`).

### `config.yaml`

Defines server and storage settings:

```yaml
server:
  port: 8000                 # HTTP port for Uvicorn
  refresh_interval: 3600     # polling interval in seconds
  store_mode: "directory"      # "memory" or "directory"
  store_directory: "./data" # only for directory mode
  export_directory: "./exports" # SPARQL result exports
  manual_import_path: "./imported_rdf" # for RDF manual imports
  log_level: "info"         # logging level (debug, info, warn, error)
```

### `zotero.yaml`

Contains the Zotero-specific settings:

```yaml
# Global RDF context (used for default vocabulary namespace)
context:
  vocab: "http://www.zotero.org/namespaces/export#"
  api_url: "https://api.zotero.org/"
  base: "https://www.zotero.org/"

# List of libraries to ingest
libraries:
  - name: "Visual Magnetism"
    api_key: "YOUR_API_KEY"
    library_type: "groups"       # "user" or "groups"
    library_id: "2536132"
    load_mode: "json"            # "json", "rdf", or "manual_import"
    rdf_export_format: "rdf_zotero"  # required only if load_mode is "rdf"

    # Optional query parameters passed to the Zotero API
    api_query_params:
      itemType: "book"
      # tag: "important"
      # collection: "XYZ123"

    # Optional RDF mapping configuration
    map:
      # Whitelist: only fields listed here (and in 'rdf_mapping') will be processed
      # white: [title, date]

      # Blacklist: fields to be explicitly ignored
      black: [title, date]

      # Fields that should be treated as structured Named Nodes
      rdf_mapping: [creators, tags, collections]

      # RDF type mapping for items
      # Fields prefixed with "_" indicate fixed RDF types
      # Others are field names whose values are interpreted as RDF types
      # Values without "http" will be expanded using the default vocab
      item_type: ["_Item", "itemType"]

      # RDF type mapping for collections (same logic as item_type)
      collection_type: ["_Collection"]

      # Additional RDF triples per item
      # - `property`: full URI or prefixed name of the predicate
      # - `value`: name of the field in the data OR a constant (if prefixed with `_`)
      # - `named_node`: if true, the value becomes a NamedNode; if false, a Literal
      item_additional:
        - property: "http://www.w3.org/2000/01/rdf-schema#label"
          value: "title"
          named_node: false
        - property: "http://www.w3.org/2002/07/owl#sameAs"
          value: "url"
          named_node: true
  - name: "Library 2"
    api_key: "YOUR_OTHER_API_KEY"
    library_type: "user"
    library_id: "ANOTHER_ID"
    load_mode: "json"
    # no rdf_export_format needed
    api_query_params:
      itemType: "article"

```

## Running

### Locally
```bash
pip install -r requirements.txt
python zotero_rdf_server.py
```

### Docker
```bash
docker-compose up --build
```

## API Endpoints

| Endpoint | Description |
|:---------|:-------------|
| `/sparql` | Run SPARQL queries (GET/POST) *(not yet implemented)* |
| `/export?format=trig` | Export full RDF dataset in TriG format |
| `/export?format=nquads` | Export full RDF dataset in N-Quads format |
| `/export?format=ttl&graph=<graph-iri>` | Export a named graph in Turtle format (only content of the given graph) |
| `/schema` | Export the Zotero item type schema as OWL ontology in JSON-LD format (incl. multilingual rdfs:label) |

### Export Parameters

- `format`: One of `trig`, `nquads`, `ttl`, `nt`, `n3`, `xml` (default: `trig`)
- `graph` *(optional)*: IRI of the named graph to export. Required for formats that do not support named graphs (e.g., `ttl`, `nt`, etc.) if you don’t want to export the default graph.

### Interactive Documentation

Visit `/docs` for the Swagger UI or `/redoc` for alternative OpenAPI documentation.

## Notes
- RDF export from Zotero API uses temporary file and bulk_load()
- Manual import mode reads local `.rdf`, `.trig`, `.ttl`, `.nt`, `.nq` files
- All Zotero entries are typed as `z:Item`
- Query parameters are configurable for fine-grained API filtering

## License

MIT License