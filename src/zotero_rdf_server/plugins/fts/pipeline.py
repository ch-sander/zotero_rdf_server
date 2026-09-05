from typing import Literal as TypeLiteral, Any, Dict, Iterator, List, Optional, Union
from pathlib import Path
from shutil import move, copy2

import json
from .helpers import plugin_logger, pipeline_log_prefix
logger=plugin_logger()

def _meta_flat_strings(d: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = str(v)
        else:
            out[k] = json.dumps(v, ensure_ascii=False)
    return out

def ingest_pipeline(
    items: list = [],
    targets: str | list = [],
    from_source: bool = True,
    framework: TypeLiteral["kraken", "tesseract", "transformer", "source", "none"] = "kraken",
    vector_kwargs: dict | None = None,
    llm_kwargs: dict | None = None,
    ingest: bool = True,
    delete_index: list = [],
    iter_pages_kwargs: dict = {},
    page_to_text_kwargs: dict = {},
    text_image_file_kwargs: dict = {},    
    config_path: str = None,
    pipeline_meta: dict = {},
    export_kwargs: dict | None = None,
):

    from .db import index_stream
    from zotero_rdf_server.utils import load_dict_like
    from .export.export_data import make_item_data, make_page_data
    from .export.export_paths import resolve_export_path
    from contextlib import nullcontext
    
    items = list(items or [])
    total = len(items)
    targets = targets or []
    iter_pages_kwargs = dict(iter_pages_kwargs or {})
    page_to_text_kwargs = dict(page_to_text_kwargs or {})
    vector_kwargs = dict(vector_kwargs or {})
    text_image_file_kwargs = dict(text_image_file_kwargs or {})
    llm_kwargs = dict(llm_kwargs or {})
    export_kwargs = dict(export_kwargs or {})
    rdf_kwargs = dict(export_kwargs.get("rdf") or {})
    qlever_kwargs = dict(export_kwargs.get("qlever") or {})
    xml_kwargs = dict(export_kwargs.get("xml") or {})
    html_kwargs = dict(export_kwargs.get("html") or {})    
    stats_kwargs = dict(export_kwargs.get("stats") or {})
    base_iri = export_kwargs.get(
        "base_iri",
        rdf_kwargs.get(
            "item_base_iri",
            rdf_kwargs.get(
                "base_iri",
                qlever_kwargs.get(
                    "base_iri",
                    xml_kwargs.get(
                        "base_iri",
                        "urn:ingest:item:",
                    ),
                ),
            ),
        ),
    )
    rdf_enabled = bool(rdf_kwargs)
    html_enabled = bool(html_kwargs)
    qlever_enabled = bool(qlever_kwargs)
    xml_enabled = bool(xml_kwargs)

    stats_enabled = bool(
        stats_kwargs
        and stats_kwargs.get("enabled", False)
    )

    logger.info(
        "Ingest Pipeline started with %s items using framework=%s...",
        total,
        framework,
    )

    page_to_text_kwargs["config_path"] = (
        config_path
        if not page_to_text_kwargs.get("config_path") and config_path
        else page_to_text_kwargs.get("config_path")
    )
    vector_kwargs["config_path"] = (
        config_path
        if not vector_kwargs.get("config_path") and config_path
        else vector_kwargs.get("config_path")
    )
    llm_kwargs["config_path"] = (
        config_path
        if not llm_kwargs.get("config_path") and config_path
        else llm_kwargs.get("config_path")
    )

    if rdf_enabled:
        try:

            from pyoxigraph import Store
            from .export.rdf_export import (
                RdfNqGzipSink,
                make_item_rdf_data,
                make_page_rdf_data,
            )
            from zotero_rdf_server.rdf import resolve_to_graph
            
        except ImportError:
            logger.exception("Import of RDF modules failed")
            rdf_enabled = False
            rdf_kwargs = {}

    if qlever_enabled:
        try:
            from .export.qlever_stream import QLeverTextGzipSink
        except ImportError:
            logger.exception("Import of QLever export modules failed")
            qlever_enabled = False
            qlever_kwargs = {}

    if xml_enabled:
        try:
            from .export.xml_export import XmlTemplateSink
        except ImportError:
            logger.exception("Import of XML export modules failed")
            xml_enabled = False
            xml_kwargs = {}

    if html_enabled:
        try:
            from .export.html_export import (
                HtmlJinjaSink,
            )
        except ImportError:
            logger.exception(
                "Import of HTML export modules failed"
            )
            html_enabled = False
            html_kwargs = {}

    if rdf_enabled:
        logger.info("RDF Export is enabled!\n")      
        rdf_output = rdf_kwargs.get("output")
        if not rdf_output:
            raise ValueError("export_kwargs['rdf'] requires 'output'")
        from zotero_rdf_server.config import EXPORT_DIRECTORY
        rdf_output = resolve_export_path(
            rdf_output,
            base_dir=EXPORT_DIRECTORY,
        )
        rdf_spec_source = rdf_kwargs.get("spec")
        if not rdf_spec_source:
            raise ValueError("export_kwargs['rdf'] requires 'spec'")

        rdf_spec = load_dict_like(rdf_spec_source)
        if not isinstance(rdf_spec, dict):
            raise TypeError("export_kwargs['rdf']['spec'] must resolve to a dict")

        rdf_context = RdfNqGzipSink(
            rdf_output,
            spec=rdf_spec,
            compresslevel=int(rdf_kwargs.get("compresslevel", 6)),
            append=bool(rdf_kwargs.get("append", False)),
        )
    else:
        rdf_context = nullcontext(None)

    if qlever_enabled:
        logger.info("QLever TSV export is enabled!\n")
        from zotero_rdf_server.config import EXPORT_DIRECTORY

        qlever_docs_output = (
            qlever_kwargs.get("docs_output")
            or qlever_kwargs.get("docs_file")
            or qlever_kwargs.get("docs")
        )
        qlever_words_output = (
            qlever_kwargs.get("words_output")
            or qlever_kwargs.get("words_file")
            or qlever_kwargs.get("words")
        )

        if not qlever_docs_output:
            raise ValueError("export_kwargs['qlever'] requires 'docs_output' or 'docs_file'")
        if not qlever_words_output:
            raise ValueError("export_kwargs['qlever'] requires 'words_output' or 'words_file'")

        qlever_docs_output = resolve_export_path(
            qlever_docs_output,
            base_dir=EXPORT_DIRECTORY,
        )
        qlever_words_output = resolve_export_path(
            qlever_words_output,
            base_dir=EXPORT_DIRECTORY,
        )

        qlever_context = QLeverTextGzipSink(
            qlever_docs_output,
            qlever_words_output,
            compresslevel=int(qlever_kwargs.get("compresslevel", 6)),
            lowercase=bool(qlever_kwargs.get("lowercase", True)),
            append=bool(qlever_kwargs.get("append", False)),
        )
    else:
        qlever_context = nullcontext(None)

    if xml_enabled:
        logger.info("XML template export is enabled!\n")
        from zotero_rdf_server.config import EXPORT_DIRECTORY

        xml_output = xml_kwargs.get("output")
        if not xml_output:
            raise ValueError("export_kwargs['xml'] requires 'output'")

        xml_spec_source = xml_kwargs.get("spec")
        if not xml_spec_source:
            raise ValueError("export_kwargs['xml'] requires 'spec'")

        xml_spec = load_dict_like(xml_spec_source)
        if not isinstance(xml_spec, dict):
            raise TypeError("export_kwargs['xml']['spec'] must resolve to a dict")

        xml_context = XmlTemplateSink(
            xml_output,
            spec=xml_spec,
            base_dir=EXPORT_DIRECTORY,
        )
    else:
        xml_context = nullcontext(None)

    if html_enabled:
        logger.info(
            "HTML Jinja export is enabled!"
        )

        from zotero_rdf_server.config import (
            EXPORT_DIRECTORY,
        )

        html_output = html_kwargs.get("output")
        if not html_output:
            raise ValueError(
                "export_kwargs['html'] requires 'output'"
            )

        html_template = html_kwargs.get(
            "template"
        )
        if not html_template:
            raise ValueError(
                "export_kwargs['html'] requires 'template'"
            )

        html_context = HtmlJinjaSink(
            html_output,
            template=html_template,
            base_dir=EXPORT_DIRECTORY,
            context=html_kwargs.get("context"),
        )
    else:
        html_context = nullcontext(None)

    def prepare_rdf_item(
        rdf_sink,
        obj,
        export_item_data=None,
    ):
        """Create one item store, emit item RDF and return graph bindings."""
        if rdf_sink is None:
            return None, None

        export_item_data = export_item_data or make_item_data(
            obj,
            base_iri=base_iri,
        )

        item_iri = export_item_data["item_iri"]
        item_data = make_item_rdf_data(
            export_item_data
        )

        graph_spec = rdf_kwargs.get("to_graph")

        if graph_spec in (None, ""):
            default_graph_uri = f"{item_iri}#graph"
        else:
            default_graph_uri = resolve_to_graph(
                graph_spec,
                data=item_data,
                node=obj,
            )

        item_store = Store()

        rdf_sink.emit(
            context="item",
            data=item_data,
            node_value=obj,
            store=item_store,
            default_graph_uri=default_graph_uri,
        )

        return item_store, default_graph_uri
    
    def dump_rdf_item(rdf_sink, item_store):
        if rdf_sink is not None and item_store is not None:
            logger.info(
                "Dumped RDF for item %s with %s quads",
                i,
                len(item_store),
            )
            rdf_sink.dump(item_store)

    logger.info(
        "Pipeline configuration:\n"
        "iter_pages_kwargs:\n%s\n\n"
        "page_to_text_kwargs:\n%s\n\n"
        "text_image_file_kwargs:\n%s\n\n"
        "llm_kwargs:\n%s\n\n"
        "export_kwargs:\n%s\n",
        json.dumps(iter_pages_kwargs, indent=2, sort_keys=True, ensure_ascii=False),
        json.dumps(page_to_text_kwargs, indent=2, sort_keys=True, ensure_ascii=False),
        json.dumps(text_image_file_kwargs, indent=2, sort_keys=True, ensure_ascii=False),
        json.dumps(llm_kwargs, indent=2, sort_keys=True, ensure_ascii=False),
        json.dumps(
            export_kwargs,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ),
    )

    if (
        not from_source
        and not ingest
        and not stats_enabled
        and not rdf_enabled
        and not qlever_enabled
        and not xml_enabled
        and not html_enabled
    ):
        return [{
            "error": (
                "nothing to do here: no from_source, no ingest, "
                "no RDF, QLever, XML or HTML export!"
            )
        }]

    vector = isinstance(vector_kwargs, dict) and vector_kwargs.get("framework")
    if vector:
        from .analysis.vector import embed
        from .helpers import clean_ocr

    use_llm = isinstance(llm_kwargs, dict) and llm_kwargs.get("tasks")
    if use_llm:
        logger.warning("LLM active!")
        from .analysis.llm import llm

    make_pages_fn = None
    client = None
    oscfg = None
    cfg_path = None

    def ensure_os_client():
        nonlocal client, oscfg, cfg_path

        if client is None:
            from .db import (
                get_os_config,
                make_client,
                resolve_config_path,
            )

            resolve_config_path.cache_clear()
            get_os_config.cache_clear()

            cfg_path = resolve_config_path(config_path)
            oscfg = get_os_config(cfg_path)
            client = make_client(oscfg)

        return client, oscfg, cfg_path

    def run_configured_stats():
        if not stats_enabled:
            return []

        from .export.stats import run_stats_for_items

        stats_client, _, _ = ensure_os_client()

        logger.info(
            "Starting Stats analysis for %s items on %s",
            len(items),
            targets,
        )

        try:
            stats_results = run_stats_for_items(
                client=stats_client,
                items=items,
                targets=targets,
                stats_cfg=stats_kwargs,
                base_iri=base_iri,
            )
        except Exception:
            logger.exception(
                "Stats analysis failed for targets=%s",
                targets,
            )

            if stats_kwargs.get("strict", False):
                raise

            return []

        written = sum(
            result.get("status") == "written"
            for result in stats_results
        )
        failed = sum(
            result.get("status") == "failed"
            for result in stats_results
        )
        skipped = sum(
            result.get("status") == "skipped"
            for result in stats_results
        )

        persisted = sum(
            result.get("status") == "persisted"
            for result in stats_results
        )

        logger.info(
            "Stats analysis completed: written=%s persisted=%s failed=%s skipped=%s",
            written,
            persisted,
            failed,
            skipped,
        )

        return stats_results

    if from_source:
        from .ocr import (
            HtmlPolicy,
            IiifOcrPolicy,
            JsonPolicy,
            PdfTextPolicy,
            TextPolicy,
            XmlPolicy,
            iter_text_pages,
        )

        ptp = iter_pages_kwargs.get("pdf_text_policy")
        if isinstance(ptp, dict):
            iter_pages_kwargs["pdf_text_policy"] = PdfTextPolicy.from_json(ptp)

        iiif_ocr_policy = iter_pages_kwargs.get("iiif_ocr_policy")
        if isinstance(iiif_ocr_policy, dict):
            iter_pages_kwargs["iiif_ocr_policy"] = IiifOcrPolicy.from_json(
                iiif_ocr_policy
            )

        text_policy = iter_pages_kwargs.get("text_policy")
        if isinstance(text_policy, dict):
            iter_pages_kwargs["text_policy"] = TextPolicy.from_json(text_policy)

        html_policy = iter_pages_kwargs.get("html_policy")
        if isinstance(html_policy, dict):
            iter_pages_kwargs["html_policy"] = HtmlPolicy.from_json(html_policy)

        xml_policy = iter_pages_kwargs.get("xml_policy")
        if isinstance(xml_policy, dict):
            iter_pages_kwargs["xml_policy"] = XmlPolicy.from_json(xml_policy)

        json_policy = iter_pages_kwargs.get("json_policy")
        if isinstance(json_policy, dict):
            iter_pages_kwargs["json_policy"] = JsonPolicy.from_json(json_policy)

        pipeline_meta["len_items"] = total

        def make_pages_fn(
            doc_id: str,
            stats: dict,
            *,
            rdf_sink=None,
            rdf_store=None,
            rdf_default_graph=None,
            qlever_sink=None,
            xml_sink=None,
            html_sink=None,
            export_item_data=None,
        ):
            def pages_fn(u: str):
                try:
                    for page_no, text in iter_text_pages(
                        u,
                        doc_id=doc_id,
                        iter_kwargs=iter_pages_kwargs,
                        page_to_text_kwargs=page_to_text_kwargs,
                        text_image_file_kwargs=text_image_file_kwargs,
                        framework=framework,
                        yield_result=(
                            ingest
                            or rdf_enabled
                            or qlever_enabled
                            or xml_enabled
                            or html_enabled
                        ),
                        pipeline_meta=pipeline_meta,
                    ):
                        page_no = int(page_no)
                        stats["pages_emitted"] += 1

                        export_page_data = make_page_data(
                            export_item_data,
                            page_no=page_no,
                            text=text,
                        )

                        if rdf_sink is not None and rdf_store is not None:
                            rdf_page_data = make_page_rdf_data(
                                export_page_data,
                            )

                            rdf_sink.emit(
                                context="page",
                                data=rdf_page_data,
                                node_value={
                                    "page": page_no,
                                    "text": text,
                                },
                                store=rdf_store,
                                default_graph_uri=rdf_default_graph,
                            )

                        if qlever_sink is not None:
                            qlever_sink.emit(
                                text=export_page_data["text"],
                                entities=(
                                    export_page_data["page_iri"],
                                ),
                            )

                        if xml_sink is not None:
                            xml_sink.emit_page(
                                export_page_data,
                                node_value={
                                    "page": page_no,
                                    "text": text,
                                },
                            )
                            
                        if html_sink is not None:
                            html_sink.emit_page(
                                export_page_data
                            )

                        yield page_no, text

                except Exception:
                    logger.exception(
                        "pages_fn failed for doc_id=%s input=%r",
                        doc_id,
                        u,
                    )
                    raise

            return pages_fn

    if not ingest:
        results: List[Dict[str, Any]] = []

        with (
            rdf_context as rdf_sink,
            qlever_context as qlever_sink,
            xml_context as xml_sink,
            html_context as html_sink,
        ):
            for i, obj in enumerate(items, start=1):
                stats = {"pages_emitted": 0}
                payload = dict(obj)
                doc_id = payload.pop("_id", None)
                input_ = payload.pop("_input", None) or payload.pop("_url", None)
                text = payload.pop("_text", "")
                sequence = int(payload.pop("_idx", 1))
                label = payload.pop("_label", "no label")
                meta = _meta_flat_strings(payload)

                pipeline_meta["i_items"] = i
                pipeline_meta["label_items"] = label

                logger.info(
                    "\n\n%s\n\n%s %s\n\n",
                    pipeline_log_prefix(pipeline_meta),
                    obj.get("_id"),
                    label,
                )

                export_item_data = make_item_data(
                    obj,
                    base_iri=base_iri,
                )

                try:
                    item_store, default_graph_uri = prepare_rdf_item(
                        rdf_sink,
                        obj,
                        export_item_data=export_item_data,
                    )

                    if xml_sink is not None:
                        xml_sink.emit_item(
                            export_item_data,
                            node_value=obj,
                        )
                    if html_sink is not None:
                        html_sink.emit_item(
                            export_item_data,
                            node_value=obj,
                        )

                    pages = []

                    if from_source:
                        if not input_:
                            raise ValueError(
                                "from_source=true requires '_input' in each item"
                            )

                        for page_no, page_text in make_pages_fn(
                            doc_id or "",
                            stats,
                            rdf_sink=rdf_sink,
                            rdf_store=item_store,
                            rdf_default_graph=default_graph_uri,
                            qlever_sink=qlever_sink,
                            xml_sink=xml_sink,
                            html_sink=html_sink,
                            export_item_data=export_item_data,
                        )(input_):
                            result_page = {
                                "page": page_no,
                                "text": page_text,
                            }

                            if vector:
                                vector_doc = embed(
                                    clean_ocr(page_text),
                                    **vector_kwargs,
                                )
                                logger.debug(vector_doc)
                                result_page["vector"] = vector_doc

                            pages.append(result_page)

                    elif text != "":
                        export_page_data = make_page_data(
                            export_item_data,
                            page_no=sequence,
                            text=text,
                        )
                        if rdf_sink is not None and item_store is not None:
                            rdf_page_data = make_page_rdf_data(
                                export_page_data,
                            )

                            rdf_sink.emit(
                                context="page",
                                data=rdf_page_data,
                                node_value={
                                    "page": sequence,
                                    "text": text,
                                },
                                store=item_store,
                                default_graph_uri=default_graph_uri,
                            )

                        if qlever_sink is not None:
                            qlever_sink.emit(
                                text=export_page_data["text"],
                                entities=(
                                    export_page_data["page_iri"],
                                ),
                            )

                        if xml_sink is not None:
                            xml_sink.emit_page(
                                export_page_data,
                                node_value={
                                    "page": sequence,
                                    "text": text,
                                },
                            )

                        if html_sink is not None:
                            html_sink.emit_page(
                                export_page_data
                            )

                        pages.append({
                            "page": sequence,
                            "text": text,
                        })
                        stats["pages_emitted"] = 1

                    if qlever_sink is not None:
                        logger.info(
                            "Exported QLever for item %s (%s): %s records/pages",
                            i,
                            doc_id,
                            stats["pages_emitted"],
                        )
                    dump_rdf_item(rdf_sink, item_store)
                    if xml_sink is not None:
                        xml_sink.emit_footer(export_item_data)
                    if html_sink is not None:
                        html_sink.emit_footer(
                            export_item_data
                        )
                except Exception as e:
                    results.append({
                        "doc_id": doc_id,
                        "label": label,
                        "input": input_,
                        "meta": meta,
                        "from_source": from_source,
                        "vector": vector,
                        "llm": use_llm,
                        "ingest": False,
                        "rdf": rdf_enabled,
                        "rdf_output": rdf_kwargs.get("output"),
                        "qlever": qlever_enabled,
                        "xml": xml_enabled,
                        "xml_output": xml_kwargs.get("output"),
                        "error": str(e),
                        "delete_index": delete_index,
                    })
                    continue

                results.append({
                    "doc_id": doc_id,
                    "label": label,
                    "input": input_,
                    "meta": meta,
                    "from_source": from_source,
                    "framework": framework,
                    "vector": vector,
                    "llm": use_llm,
                    "ocr_pages": len(pages),
                    "ingest": False,
                    "rdf": rdf_enabled,
                    "rdf_output": rdf_kwargs.get("output"),
                    "qlever": qlever_enabled,
                    "xml": xml_enabled,
                    "xml_output": xml_kwargs.get("output"),
                    "targets": targets,
                    "delete_index": delete_index,
                })

        run_configured_stats()
        logger.info("Pipeline finished with %s results!", len(results))
        return results

    runs: List[dict] = []

    client, oscfg, cfg_path = ensure_os_client()

    if delete_index:
        targets_list = (
            [delete_index]
            if isinstance(delete_index, str)
            else list(delete_index)
        )
        for target in targets_list:
            response = client.indices.delete(
                index=str(target),
                ignore=[400, 404],
            )
            logger.warning("\n\nDeleted Index %s: %s\n\n", target, response)

    from .db import provision_from_cfg

    try:
        logger.info("Provisioning %s...", targets)
        provision_from_cfg(client, oscfg)
        logger.info("Provisioning completed!")
    except Exception:
        logger.exception("OpenSearch provisioning failed")
        raise

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    with (
        rdf_context as rdf_sink,
        qlever_context as qlever_sink,
        xml_context as xml_sink,
        html_context as html_sink,
    ):
        for i, obj in enumerate(items, start=1):
            stats = {"pages_emitted": 0}
            payload = dict(obj)
            logger.debug("Ingest Pipeline payload: %s", payload)

            doc_id = payload.pop("_id", None)
            input_ = payload.pop("_input", None)
            text = payload.pop("_text", "")
            sequence = int(payload.pop("_idx", 1))
            label = payload.pop("_label", "no label")
            meta = _meta_flat_strings(payload)

            pipeline_meta["i_items"] = i
            pipeline_meta["label_items"] = label

            logger.info(
                "\n\n%s\n%s %s\n\n",
                pipeline_log_prefix(pipeline_meta),
                obj.get("_id"),
                label,
            )
            logger.debug(
                "Ingest Pipeline index_stream from source: %s",
                from_source,
            )

            item_store = None
            default_graph_uri = None
            export_item_data = make_item_data(
                obj,
                base_iri=base_iri,
            )

            try:
                item_store, default_graph_uri = prepare_rdf_item(
                    rdf_sink,
                    obj,
                    export_item_data=export_item_data,
                )

                if xml_sink is not None:
                    xml_sink.emit_item(
                        export_item_data,
                        node_value=obj,
                    )

                if html_sink is not None:
                    html_sink.emit_item(
                        export_item_data,
                        node_value=obj,
                    )

                if from_source:
                    if not input_:
                        logger.error(
                            "from_source=true requires '_input' in each item"
                        )
                        continue

                    logger.info("Ingest Pipeline with input from source!")
                    digest = index_stream(
                        client=client,
                        oscfg=oscfg,
                        input=input_,
                        doc_id=doc_id,
                        label=label,
                        url_to_text_pages_fn=make_pages_fn(
                            doc_id or "",
                            stats,
                            rdf_sink=rdf_sink,
                            rdf_store=item_store,
                            rdf_default_graph=default_graph_uri,
                            qlever_sink=qlever_sink,
                            xml_sink=xml_sink,
                            html_sink=html_sink,
                            export_item_data=export_item_data,
                        ),
                        targets=targets,
                        meta=meta,
                        vector_kwargs=vector,
                        llm_kwargs=llm_kwargs,
                    )

                    digest["from_source"] = True
                    digest["framework"] = framework
                    digest["ocr_pages"] = stats["pages_emitted"]

                else:
                    logger.info("Ingest Pipeline with no input from source!")
                    d: Dict[str, Any] = {
                        "ingest_ts": now,
                        "meta": meta,
                    }

                    if input_ is not None:
                        d["input"] = input_
                    if doc_id is not None:
                        d["doc_id"] = doc_id
                    if sequence is not None:
                        d["page"] = sequence
                    if text != "":
                        d["text"] = text
                    if label != "":
                        d["label"] = label

                    if text != "":
                        export_page_data = make_page_data(
                            export_item_data,
                            page_no=sequence,
                            text=text,
                        )

                        if rdf_sink is not None and item_store is not None:
                            rdf_page_data = make_page_rdf_data(
                                export_page_data,
                            )
                            rdf_sink.emit(
                                context="page",
                                data=rdf_page_data,
                                node_value={
                                    "page": sequence,
                                    "text": text,
                                },
                                store=item_store,
                                default_graph_uri=default_graph_uri,
                            )

                        if qlever_sink is not None:
                            qlever_sink.emit(
                                text=export_page_data["text"],
                                entities=(
                                    export_page_data["page_iri"],
                                ),
                            )

                        if xml_sink is not None:
                            xml_sink.emit_page(
                                export_page_data,
                                node_value={
                                    "page": sequence,
                                    "text": text,
                                },
                            )

                        if html_sink is not None:
                            html_sink.emit_page(
                                export_page_data
                            )

                        stats["pages_emitted"] = 1

                    if vector:
                        vector_doc = embed(
                            clean_ocr(text),
                            **vector_kwargs,
                        )
                        d["vector"] = vector_doc

                    if use_llm:  # TODO adjust
                        llm_kwargs_item = dict(llm_kwargs)
                        llm_mapping_key = llm_kwargs_item.pop(
                            "mapping_key",
                            "llm",
                        )
                        llm_mapping_keys = (
                            llm_kwargs_item.pop("mapping_keys", None)
                            or [llm_mapping_key]
                        )
                        llm_response = llm(
                            clean_ocr(text),
                            llm_kwargs_item,
                        )
                        logger.debug(llm_response)
                        llm_dict = load_dict_like(llm_response)
                        if llm_dict:
                            for key in llm_mapping_keys:
                                d[key] = llm_response
                            logger.debug(
                                json.dumps(llm_dict, indent=4)
                            )

                    digest = index_stream(
                        client=client,
                        oscfg=oscfg,
                        targets=targets,
                        doc_id=doc_id,
                        doc=d,
                    )

                    digest["from_source"] = False
                    digest["framework"] = framework
                    digest["ocr_pages"] = stats["pages_emitted"]

                dump_rdf_item(rdf_sink, item_store)
                if xml_sink is not None:
                    xml_sink.emit_footer(export_item_data)
                if qlever_sink is not None:
                    logger.info(
                        "Exported QLever for item %s (%s): %s records/pages",
                        i,
                        doc_id,
                        stats["pages_emitted"],
                    )
                if html_sink is not None:
                    html_sink.emit_footer(
                        export_item_data
                    )
                digest["ingest"] = True
                digest["delete_index"] = delete_index
                digest["llm"] = bool(use_llm)
                digest["rdf"] = rdf_enabled
                digest["rdf_output"] = rdf_kwargs.get("output")
                digest["qlever"] = qlever_enabled
                digest["xml"] = xml_enabled
                digest["xml_output"] = xml_kwargs.get("output")
                digest["html"] = html_enabled
                digest["html_output"] = html_kwargs.get("output")
                runs.append(digest)

            except Exception as e:
                logger.exception(
                    "Ingest failed for doc_id=%s",
                    doc_id,
                )
                runs.append({
                    "doc_id": doc_id,
                    "label": label,
                    "from_source": from_source,
                    "ingest": True,
                    "rdf": rdf_enabled,
                    "rdf_output": rdf_kwargs.get("output"),
                    "qlever": qlever_enabled,
                    "xml": xml_enabled,
                    "xml_output": xml_kwargs.get("output"),
                    "error": str(e),
                    "delete_index": delete_index,
                })

    run_configured_stats()
    logger.info("Ingest Pipeline finished with %s runs!", len(runs))
    return runs

Action = TypeLiteral["delete", "move", "copy"]

def clean_files(
    root_dir: str | Path,
    extension: str,
    min_bytes: int | None = None,
    max_bytes: int | None = None,
    min_content_len: int | None = None,
    max_content_len: int | None = None,
    action: Action = "copy",
    move_to: str | Path | None = None,
    all_files: bool = False,
) -> dict:
    """
    Recursively finds files by extension and deletes, moves or copies files
    matching the configured ranges.

    Range semantics are inclusive:

        min_bytes <= file size <= max_bytes

        min_content_len <= decoded content length <= max_content_len

    Missing bounds are ignored.

    If both a byte range and a content-length range are configured, a file
    must match both ranges.

    If all_files=True, all matching files are processed regardless of ranges.
    """

    root = Path(root_dir).resolve()

    if not root.exists() or not root.is_dir():
        logger.error(f"root_dir does not exist or is not a directory: {root}")
        return {
            "root_dir": str(root),
            "extension": extension,
            "action": action,
            "deleted": 0,
            "moved": 0,
            "copied": 0,
            "skipped": 0,
            "errors": [],
            "directory_missing": True,
        }
        # raise ValueError(f"root_dir does not exist or is not a directory: {root}")

    valid_actions = {"delete", "move", "copy"}

    if action not in valid_actions:
        raise ValueError(
            f"Unsupported action {action!r}; expected one of "
            f"{sorted(valid_actions)}"
        )

    if min_bytes is not None and min_bytes < 0:
        raise ValueError("min_bytes must be >= 0")

    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must be >= 0")

    if min_content_len is not None and min_content_len < 0:
        raise ValueError("min_content_len must be >= 0")

    if max_content_len is not None and max_content_len < 0:
        raise ValueError("max_content_len must be >= 0")

    if (
        min_bytes is not None
        and max_bytes is not None
        and min_bytes > max_bytes
    ):
        raise ValueError(
            "min_bytes must be less than or equal to max_bytes"
        )

    if (
        min_content_len is not None
        and max_content_len is not None
        and min_content_len > max_content_len
    ):
        raise ValueError(
            "min_content_len must be less than or equal to "
            "max_content_len"
        )

    has_byte_filter = min_bytes is not None or max_bytes is not None
    has_content_filter = (
        min_content_len is not None
        or max_content_len is not None
    )

    if not all_files and not has_byte_filter and not has_content_filter:
        raise ValueError(
            "At least one range filter or all_files=True must be set"
        )

    if not extension.startswith("."):
        extension = f".{extension}"

    target_dir: Path | None = None

    if action in {"move", "copy"}:
        if move_to is None:
            raise ValueError(
                "move_to must be set when action='move' or action='copy'"
            )

        target_dir = Path(move_to).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)

    deleted = 0
    moved = 0
    copied = 0
    skipped = 0
    errors: list[dict[str, str]] = []

    for file_path in root.rglob(f"*{extension}"):
        if not file_path.is_file():
            continue

        try:
            if all_files:
                should_clean = True
            else:
                checks: list[bool] = []

                if has_byte_filter:
                    file_size = file_path.stat().st_size

                    byte_matches = (
                        (min_bytes is None or file_size >= min_bytes)
                        and
                        (max_bytes is None or file_size <= max_bytes)
                    )

                    checks.append(byte_matches)

                if has_content_filter:
                    try:
                        content = file_path.read_text(
                            encoding="utf-8",
                            errors="ignore",
                        )
                    except OSError as exc:
                        errors.append({
                            "file": str(file_path),
                            "error": str(exc),
                        })
                        continue

                    content_len = len(content)

                    content_matches = (
                        (
                            min_content_len is None
                            or content_len >= min_content_len
                        )
                        and
                        (
                            max_content_len is None
                            or content_len <= max_content_len
                        )
                    )

                    checks.append(content_matches)

                # Every configured filter must match.
                should_clean = all(checks)

            if not should_clean:
                skipped += 1
                continue

            if action == "delete":
                file_path.unlink()
                deleted += 1
                continue

            assert target_dir is not None

            relative_path = file_path.relative_to(root)
            destination = target_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)

            if action == "move":
                move(str(file_path), str(destination))
                moved += 1
            else:
                copy2(str(file_path), str(destination))
                copied += 1

        except OSError as exc:
            errors.append({
                "file": str(file_path),
                "error": str(exc),
            })

    return {
        "root_dir": str(root),
        "extension": extension,
        "action": action,
        "filters": {
            "min_bytes": min_bytes,
            "max_bytes": max_bytes,
            "min_content_len": min_content_len,
            "max_content_len": max_content_len,
            "all_files": all_files,
        },
        "deleted": deleted,
        "moved": moved,
        "copied": copied,
        "skipped": skipped,
        "errors": errors,
    }

# END