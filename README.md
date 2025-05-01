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

Set your `config.yaml` in `.env`!

You may set up multiple Zotero libraries in your config. Each library will be loaded in the database as named graph (graph URI is the library URI, i.e. `https://api.zotero.org/{library_type}/{library_id}`)

Example `config.yaml`:

```yaml
zotero:
  - name: Library 1 Name # only internal use, does not have to be the name given in Zotero
    api_key: "YOUR_API_KEY"
    library_type: "groups"  # "user" or "groups"
    library_id: "YOUR_LIBRARY_ID"
    load_mode: "rdf"  # "json" or "rdf" or "manual_import"
    rdf_export_format: "rdf_zotero" # "rdf_zotero" "rdf_bibliontology" only needed if load_mode = "rdf"
    api_query_params:
      itemType: "book"  # optional, freely configurable
      # tag: "important"  # optional, freely configurable
      # collection: "XYZ123"  # optional
  - name: Library 2 Name # only internal use, does not have to be the name given in Zotero
    api_key: "YOUR_API_KEY"
    library_type: "groups"  # "user" or "groups"
    library_id: "YOUR_LIBRARY_ID"
    load_mode: "rdf"  # "json" or "rdf" or "manual_import"
    rdf_export_format: "rdf_zotero" # "rdf_zotero" "rdf_bibliontology" only needed if load_mode = "rdf"
    api_query_params:
      itemType: "book"  # optional, freely configurable
      # tag: "important"  # optional, freely configurable
      # collection: "XYZ123"  # optional

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
| `/sparql` | Run SPARQL queries (GET/POST) *(not yet implemented)* |
| `/export?format=trig` | Export full RDF dataset in TriG format |
| `/export?format=nquads` | Export full RDF dataset in N-Quads format |
| `/export?format=ttl&graph=<graph-iri>` | Export a named graph in Turtle format (only content of the given graph) |

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