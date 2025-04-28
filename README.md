# Zotero RDF SPARQL Server (Pyoxigraph-only)

This server loads a complete Zotero library into an RDF graph,
exposes a local SPARQL endpoint, and allows exporting the graph.

## Features
- Load mode: JSON (Zotero API), RDF (Zotero API), or manual RDF import
- Only Pyoxigraph in bulk-load where possible
- Configurable API Query parameters (e.g., itemType, tag, collection)
- Always using z:Item as type (no separate Note/Item types)
- Correct Zotero export namespace
- FastAPI SPARQL endpoint not yet deployed
- Export as TriG or N-Quads
- Docker and Compose ready
- includes Oxigraph server in docker to query at port 7878

## Configuration

Example `config.yaml`:

```yaml
zotero:
  api_key: "YOUR_API_KEY"
  library_type: "users"
  library_id: "YOUR_LIBRARY_ID"
  load_mode: "json"
  rdf_export_format: "rdf_zotero"
  api_query_params:
    itemType: "note"
    tag: "important"

server:
  port: 8000
  refresh_interval: 3600
  store_mode: "memory"
  store_directory: "./data"
  export_directory: "./exports"
  manual_import_path: "./imported_rdf"
  log_level: "info"
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
| `/sparql` | Run SPARQL queries (GET/POST) (not yet running) |
| `/export?format=trig` | Export graph as TriG |
| `/export?format=nquads` | Export graph as N-Quads |

## Notes
- RDF export from Zotero API uses temporary file and bulk_load()
- Manual import mode reads local `.rdf`, `.trig`, `.ttl`, `.nt`, `.nq` files
- All Zotero entries are typed as `z:Item`
- Query parameters are configurable for fine-grained API filtering

## License

MIT License