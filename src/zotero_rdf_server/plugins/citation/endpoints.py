from pathlib import Path
from zotero_rdf_server.utils import ensure_import, load_text_like
from fastapi import Response, APIRouter, HTTPException, Query
from zotero_rdf_server.config import CFF_PATH

here = Path(__file__).resolve().parent
requirements = here / "requirements.txt"

open_router = APIRouter(tags=["Citation"])

@open_router.get("/render")
def citation(
    method: str = Query(
        default="as_apalike",
        description="method for cffconvert package",
    ),
    path: str = Query(
        default=str(CFF_PATH),
        description="Path to CITATION.cff",
    ),
):
    
    cffconvert = ensure_import(
        "cffconvert",
        requirements=requirements,
    )
    cff_path = Path(path)
    if not cff_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"CITATION.cff not found: {cff_path}",
        )

    if not cff_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Path is not a file: {cff_path}",
        )
    try:
        cff_path = Path(path).expanduser().resolve()
        cff_yaml = load_text_like(cff_path)
        cff = cffconvert.Citation(cff_yaml)
        return Response(getattr(cff, method)())
    except Exception as e:
        raise HTTPException(
            status_code=422,
            detail=f"CITATION.cff could not be converted with {method}: {e}",
        ) from e