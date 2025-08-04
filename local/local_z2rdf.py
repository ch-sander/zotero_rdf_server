import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv
from fastapi.openapi.utils import get_openapi

script_dir = Path(__file__).resolve().parent
os.chdir(script_dir)
print("script_dir =", script_dir)
ENV_PATH = Path(".env.local").resolve()
print(f"ENV_PATH = {ENV_PATH}")

load_dotenv(dotenv_path=ENV_PATH, override=True)
WORKDIR = Path(os.getenv("WORKDIR")).resolve()
SRC_PATH = WORKDIR / "src"
print(f"SRC_PATH = {SRC_PATH}")
sys.path.insert(0, str(SRC_PATH))
os.chdir(SRC_PATH)
print("WORKDIR =", Path.cwd().resolve())

from zotero_rdf_server.main import app

def generate_openapi(output_path: Path, html_output_path: Path) -> None:
    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
        description=app.description
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2, ensure_ascii=False)
    print(f"✅ openapi.json written to {output_path}")
    html = f"""<!DOCTYPE html>
    <html>
    <head>
        <title>API Docs</title>
        <meta charset="utf-8"/>
        <script src="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"></script>
    </head>
    <body>
        <div id="redoc"></div>
        <script>
        const spec = {json.dumps(schema, indent=2)};
        Redoc.init(spec, {{}}, document.getElementById('redoc'));
        </script>
    </body>
    </html>
    """

    with open(html_output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ Redoc HTML with embedded API documentation generated: {html_output_path}")

import asyncio

async def export_rdf(force=False):
    from zotero_rdf_server.store import initialize_store, refresh_store
    initialize_store()
    print(f"✅ Store initialized")

    refresh_store(force)
    print(f"✅ Store reloaded")

    from zotero_rdf_server.api import export_graph
    exp = await export_graph(format="trig", graph=None)
    print(f"✅ {exp}")



def main():
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_openapi = subparsers.add_parser("generate_openapi")
    parser_openapi.add_argument("--output", type=Path, default=WORKDIR / "docs" / "openapi.json")
    parser_openapi.add_argument("--html-output", type=Path, default=WORKDIR / "docs" / "openapi.html")

    parser_rdf = subparsers.add_parser("export_rdf")
    parser_rdf.add_argument("--force", action="store_true", help="Force reloading Store")

    args = parser.parse_args()

    if args.command == "generate_openapi":
        generate_openapi(output_path=args.output, html_output_path=args.html_output)

    elif args.command == "export_rdf":
        asyncio.run(export_rdf(force=args.force))

if __name__ == "__main__":
    main()