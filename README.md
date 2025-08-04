# Zotero RDF Server

This server loads multiple Zotero libraries into an RDF graph,
exposes a local SPARQL endpoint, and allows exporting the graph.
A **visual query builder** is found in `/explorer` to explore the graph or go to [GitHub Pages](https://ch-sander.github.io/zotero_rdf_server/).

### Why this Tool?

While Zotero offers robust functionality for storing and collaboratively managing cloud-hosted libraries, it lacks support for federated access and cross-library exploration or search.
This **Zotero RDF Server** is an initial attempt to fill that gap. It implements basic entity mapping (e.g., tags, creators), but remains tightly constrained by Zotero’s inherently textual data model and API structure.
A logical next step would be to implement a **knowledge base mapping** layer to enable richer semantic interoperability.

<details>
<summary>📘 How to Create a Zotero Cloud Library</summary>

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
</details>
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

## Parse Notes

As a plugin, you can parse your HTML Zotero notes with the [Semantic-HTML](https://github.com/ch-sander/semantic-html) package ([Docs](https://semantic-html.readthedocs.io/en/latest/)). It is only loaded if the trigger is set in the `config.yaml` or called via `/parse_notes` in the API. The results are parsed as RDF and loaded to the store. A mapping example for the RDF parsing is defined in `app/parser/mapping.json` and can be specified in `config.yaml` for each library

## Configuration

Place both YAML filenames in your `.env` (example in [.env.backup](.env.backup)), not in the code or Dockerfile.
Docker-Compose will mount these files into `/app` (or any other directory set in `.env`) and your Python code loads them via `os.getenv(...)` with sensible defaults (`config.yaml` and `zotero.yaml`).

### `config.yaml`

Defines server and storage settings, see [app/config.yaml](app/config.yaml) as an example with comments.

### `zotero.yaml`

Contains the Zotero-specific settings, see [app/zotero.yaml](app/zotero.yaml) as an example with comments.

## Running

### Locally

```bash
pip install -r requirements.txt
python src/zotero_rdf_server/main.py
```

You can even run it as a GitHub Action for RDF export, see [local/local_z2rdf.py](local/local_z2rdf.py) and [.github/workflows/rdf_export.yml](.github/workflows/rdf_export.yml)


### Docker

```bash
docker-compose up --build -d
```

## API

Visit `/` when the app is running for the Swagger UI or `/redoc` for alternative OpenAPI documentation.

A static documentation (OpenAPI) is found in [docs/openapi.json](docs/openapi.json) and [HTML](https://raw.githack.com/ch-sander/zotero_rdf_server/main/docs/openapi.html)



## License

MIT License