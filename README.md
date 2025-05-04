# Zotero RDF Server

This server loads multiple Zotero libraries into an RDF graph,
exposes a local SPARQL endpoint, and allows exporting the graph.
## 📘 How to Create a Zotero Cloud Library

To use this tool, you need at least one Zotero cloud library (either **user** or **group**). Here’s how to set it up:

1. **Create a Zotero Account**  
   Sign up at [https://www.zotero.org/user/register](https://www.zotero.org/user/register)

2. **Install Zotero** *(optional but recommended)*  
   Download from [https://www.zotero.org/download](https://www.zotero.org/download)

3. **Create a Library**
   - **User Library**: Log in and add items directly to your personal Zotero library.
   - **Group Library**:
     - Go to [https://www.zotero.org/groups](https://www.zotero.org/groups)
     - Click **Create a New Group**
     - Choose visibility and permissions
     - Add items via the Zotero client or web interface

4. **Find your Library ID**
   - Visit your group library online (e.g. `https://www.zotero.org/groups/2536132/your-group-name`)
   - The number in the URL is your `library_id`.

5. **Create an API Key**
   - Go to [https://www.zotero.org/settings/keys](https://www.zotero.org/settings/keys)
   - Click **Create new private key**
   - Select the appropriate access level (e.g., read-only)

👉 More help in the official docs:  
[Zotero Web Library](https://www.zotero.org/support/web_library)  
[Groups](https://www.zotero.org/support/groups)  
[API Guide](https://www.zotero.org/support/dev/web_api/v3/start)

---

## Features

- Load modes: JSON (via Zotero API), RDF (via API), or manual RDF import
- Efficient graph loading using Pyoxigraph wherever possible
- Configurable API query parameters (e.g., `itemType`, `tag`, `collection`)
- Correct Zotero RDF namespace handling
- Export as TriG or N-Quads
- Docker and Compose support
- Includes Oxigraph SPARQL server at port `7878`
- *(FastAPI endpoint for `/sparql` not yet implemented)*

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
  schema: "https://api.zotero.org/schema" # If specified, will generate a basic OWL ontology as a named graph using the IRI from vocab

libraries:
  - name: My Library # Only required for "manual_import" as a subdirectory containing RDF files
    api_key: "xxxx"
    library_type: "groups"  # "user" or "groups"
    library_id: "123"
    load_mode: "manual_import"  # Options: "json", "rdf", or "manual_import"
    rdf_export_format: "rdf_zotero" # Options: "rdf_zotero", "rdf_bibliontology"; only needed if load_mode = "rdf"
    # base_uri: "https://www.example.com#" Used as the URI for the library's named graph and as the base URI for all named nodes created for Zotero items and collections. Defaults to "{context.base}{libraries.library_type}/{libraries.library_id}" as defined in this YAML
    # uuid_namespace: "https://www.example.com#" Used to generate consistent UUIDs for named nodes across multiple libraries in the union graph. Defaults to base_uri if not specified
    map: # Skip this block if no specifications are needed. Empty lists will be ignored
      # white: [title, date] # Whitelist – only include these fields and those in 'named'
      black: [title, date] # Blacklist – exclude these fields
      rdf_mapping: [creators, tags, collections]
      item_type: ["_Item", "itemType"] # Determines RDF type; leading underscore indicates a constant predicate. If not specified, defaults to "Item". If not starting with "http", the default vocab from context will be used
      collection_type: ["_Collection"] # If not specified, defaults to "Collection"
      named_library: "inLibrary" # If specified, adds an object property with this name linking to the library's named graph URI to support querying across named graphs
      item_additional:
        - property: "http://www.w3.org/2000/01/rdf-schema#label"
          value: "title"
          named_node: false
        - property: "http://www.w3.org/2002/07/owl#sameAs"
          value: "url"
          named_node: true
    api_query_params:
      itemType: "book"  # Optional, freely configurable
      # tag: "important"  # Optional, freely configurable
      # collection: "XYZ123"  # Optional

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