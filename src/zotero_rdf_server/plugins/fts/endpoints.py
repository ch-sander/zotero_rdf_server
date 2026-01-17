from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse
from typing import Literal as TypeLiteral
import logging
from pathlib import Path
from zotero_rdf_server.store import *
from zotero_rdf_server.rdf import *
from zotero_rdf_server.logging_config import logger, LogLevel
from zotero_rdf_server.config import *
from zotero_rdf_server.models import ZoteroLibrary
from zotero_rdf_server.utils import *


router = APIRouter()

@router.get("/fts", summary="fts", description="fts", tags=["Plugins"])
async def fts():

    return