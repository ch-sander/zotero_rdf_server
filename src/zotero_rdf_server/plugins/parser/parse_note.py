import subprocess
import sys, json, html
from uuid import uuid5, NAMESPACE_URL, uuid4
import json, re
from datetime import datetime
from dateutil import parser
from pathlib import Path
from zotero_rdf_server.global_store import Store, Quad, NamedNode, Literal, RdfFormat, BlankNode
from zotero_rdf_server.logging_config import logger
from zotero_rdf_server.config import *
from zotero_rdf_server.models import ZoteroLibrary
from zotero_rdf_server.utils import *
from zotero_rdf_server.rdf import load_rdf_from_spec

here = Path(__file__).resolve().parent
requirements = here / "requirements.txt"

semantic_parse_note = ensure_import(
    "semantic_html.parser",
    attr="parse_note",
    requirements=requirements,
)


class ParseNotePlugin:
    def __init__(self, mapping: dict | None, metadata: dict = None):
        self.mapping = mapping
        self.metadata = metadata or {}
        if not mapping:
            logger.error("No config for parser provided.")
            raise ValueError("No config for parser provided.") 

    def run(
        self,
        html_str: str,
        note_uri: str,
        rdfa:bool=False,
        wadm:bool=False
    ) -> dict:
        logger.debug(f"Parsing HTML note for URI: {note_uri}")
        logger.debug(f"Unescaping HTML")

        html_str = html.unescape(html_str)

        result = semantic_parse_note (
            html_input=html_str,
            mapping=self.mapping,
            note_uri=note_uri,
            metadata=self.metadata,
            rdfa=rdfa,
            wadm=wadm
        )
        logger.debug("Parsing completed.")
        return result

def parse_all_notes(lib: ZoteroLibrary, store: Store, note_predicate : NamedNode = NamedNode(f"{ZOT_NS}note"), query_str: str = None, delete:bool = False, push:bool=True):
    # from zotero_rdf_server.plugins.parser.parse_note import ParseNotePlugin
    parser_cfgs = lib.plugin.get("notes_parser") or []
    GRAPH_URI = safeNamedNode(lib.base_url) # Source graph of notes
    KB_GRAPH = safeNamedNode(lib.knowledge_base_graph) # graph to link SEMANTIC_HTML_GRAPH entites to
    MAP_GRAPH = safeNamedNode(lib.mapping_base_graph)
    count = 0
    parser_cfgs = [parser_cfgs] if isinstance(parser_cfgs,dict) else parser_cfgs
    if len(parser_cfgs)>1:
        logger.warning(f"Running {len(parser_cfgs)} Notes Parser configurations for library {lib.base_url}")
    for parser_cfg in parser_cfgs: # allow multiple runs per library    
        SEMANTIC_HTML_GRAPH = safeNamedNode(parser_cfg.get("to_graph", parser_cfg.get("base_uri", lib.base_url))) # graph to store RDF parsed from notes
        
        mapping = load_dict_like(
            parser_cfg.get("mapping"),
            default={"@context": {"@base": lib.base_url, "@vocab": ZOT_NS}}, # TODO fallback not sufficient
            label="Parsing Semantic HTML mapping",
            verbose=True
        )

        metadata = load_dict_like(
            parser_cfg.get("metadata"),
            default={"http://www.w3.org/ns/prov#wasGeneratedBy": lib.user},
            label="Parsing Semantic HTML  metadata",
            verbose=True
        )

        map_KB = parser_cfg.get("knowledge_base_mapping", False)
        tag_filter = parser_cfg.get("tag_filter")
        predicate = parser_cfg.get("predicate")
        query = parser_cfg.get("query")
        if predicate and isinstance(predicate, str): note_predicate = safeNamedNode(predicate)
        if query and isinstance(query, str) and "SELECT" in str(query).upper(): query_str = query


        if map_KB:        
            fuzzy_threshold = parser_cfg.get("fuzzy", 90)
            knowledge_base = mapping.pop("KnowledgeBase", [])
            # entity_graph_uri = safeNamedNode(lib.knowledge_base_graph)
            logger.debug(f"Map semantic entites to KB following: {knowledge_base}")

        def map_semantic_entities(
            source_store,
            target_store,
            knowledge_base: list = None,
            fuzzy_threshold: int = 85
        ):
            result_store = Store()

            def _subjects(store, graph=None):
                return {q.subject for q in store.quads_for_pattern(None, None, None, graph)}

            if knowledge_base is None:
                return result_store
            for rule in knowledge_base:
                fuzzy_rules = rule.get("FUZZY", [])
                pool_rules = rule.get("POOL", [])
                same_rules = rule.get("SAME", [])
                type_source = rule.get("entityTypeSource","mapping_or_rule")
                map_prop = safeNamedNode(rule.get("mapProperty", OWL_SAME_AS))
                KB_graph = rule.get("knowledgeBaseGraph", None)
                mapping_graph = rule.get("mappingBaseGraph", None)
                entity_graph_uri = safeNamedNode(KB_graph) if KB_graph else KB_GRAPH
                mapping_graph_uri = safeNamedNode(mapping_graph) if mapping_graph else MAP_GRAPH
                map_label_prop = safeNamedNode(rule.get("mapLabel", MAP_LABEL))
                add_jsonld = rule.get("ADD")
                allow_create = rule.get("allowCreate", False)
                # AND
                filter_source_subjects = set()
                filter_target_subjects = set()
                filter_source_store = Store()
                filter_target_store = Store()
                logger.debug("KB definition found!")
                type_hints = set()
                if pool_rules:
                    logger.debug("POOL Rule definition found!")
                    for p in pool_rules:

                        operator = p.get('operator', "OR")
                        comment = p.get('comment')
                        if comment and isinstance(comment,str):
                            logger.debug(comment)
                        domainProperty = p.get('domainProperty', None)
                        domainObject = p.get('domainObject', None)                    
                        targetProperty = p.get('targetProperty', None)
                        targetObject = p.get('targetObject', None)
                        if targetProperty == RDF_TYPE and targetObject is not None:
                            type_hints.add(targetObject)
                        if domainProperty or domainObject:
                            if operator == "AND":
                                pool_source = filter_source_store
                            else:
                                pool_source = source_store

                            src_qs = list(pool_source.quads_for_pattern(
                                None,
                                safeNamedNode(domainProperty, allow_None = True),
                                safeNamedNode(domainObject, allow_None = True),
                                None
                            ))

                            filter_source_subjects.update(q.subject for q in src_qs)
                            logger.info(f"POOL rule found (domain). Added {len(filter_source_subjects)}")
                            for q in src_qs:
                                filter_source_store.bulk_extend(pool_source.quads_for_pattern(q.subject, None, None, None))                        
                        else:
                            logger.warning(f"No domain POOL rule applied, couldn't find {domainProperty} or {domainObject}")

                        if targetProperty or targetObject:
                            if operator == "AND":
                                pool_target = filter_target_store
                            else:
                                pool_target = target_store
                            
                            tgt_qs = list(pool_target.quads_for_pattern(
                                None,
                                safeNamedNode(targetProperty, allow_None = True),
                                safeNamedNode(targetObject, allow_None = True),
                                entity_graph_uri
                            ))

                            filter_target_subjects.update(q.subject for q in tgt_qs)
                            logger.info(f"POOL rule found (target). Added {len(filter_target_subjects)}")
                            for q in tgt_qs:
                                filter_target_store.bulk_extend(pool_target.quads_for_pattern(q.subject, None, None, entity_graph_uri))
                        else:
                            logger.warning(f"No target POOL rule applied, couldn't find {targetProperty} or {targetObject}")

                else: # fallback if no POOL rules
                    logger.debug("No POOL Rule definition found!")
                    filter_source_subjects = _subjects(source_store, None)
                    filter_target_subjects = _subjects(target_store, entity_graph_uri)

                    
                logger.info(f"LEN filtered source store: {len(filter_source_store)}. Found graphs in filtered source store: {list(filter_source_store.named_graphs())}")

                logger.info(f"LEN filtered target store: {len(filter_target_store)}. Found graphs in filtered target store: {list(filter_target_store.named_graphs())}")

                if same_rules:
                    logger.debug(f"{len(same_rules)} SAME Rules found!")
                if fuzzy_rules:
                    logger.debug(f"{len(fuzzy_rules)} FUZZY Rules found!")

                for domain_node in filter_source_subjects:
                    value_matched = False

                    # Add to Annotation from Config
                    if parser_cfg.get("load"):
                        load_rdf_from_spec(
                            parser_cfg,
                            context=None,
                            data={
                                "record_id": stable_int_id(domain_node.value),
                            },
                            node_value=domain_node.value,
                            store=result_store,
                            default_graph_uri=SEMANTIC_HTML_GRAPH,
                        )                        

                    # SAME
                    for same_rule in same_rules:                    
                        try:
                            dom_prop = safeNamedNode(same_rule["domainProperty"])
                            tgt_prop = safeNamedNode(same_rule["targetProperty"])
                        except KeyError:
                            continue

                        for dp in filter_source_store.quads_for_pattern(domain_node, dom_prop, None):
                            lit_value = str(dp.object.value)

                            for tq in filter_target_store.quads_for_pattern(None, tgt_prop, Literal(lit_value), entity_graph_uri):
                                result_store.add(Quad(
                                    domain_node,
                                    map_prop,
                                    tq.subject,
                                    dp.graph_name
                                ))
                                logger.debug(f"[SAME] Matched {lit_value} by identity: {domain_node} → {tq.subject}")
                                value_matched = True

                                entry = ensure_entry(result_store, tq.subject, map_graph=mapping_graph_uri, type_hints=list(type_hints))
                                ensure_mapping_literal(result_store, entry, lit_value, map_label_prop, mapping_graph_uri)                           

                                break
                            if value_matched:
                                break
                        if value_matched:
                            break

                    if value_matched:
                        continue  # no FUZZY needed

                    # FUZZY
                    for fuzzy in fuzzy_rules:
                        try:
                            domain_prop = safeNamedNode(fuzzy["domainProperty"])
                            target_prop = safeNamedNode(fuzzy["targetProperty"])
                            fuzzy_threshold = fuzzy.get('threshold', fuzzy_threshold)
                            regex = fuzzy.get('regex', False)
                        except KeyError:
                            continue

                        # logger.info("############################################")
                        # logger.info(len(filter_source_store))

                        for dp in filter_source_store.quads_for_pattern(domain_node, domain_prop, None):
                            lit_value = str(dp.object.value)

                            try:
                                
                                pool_map = Store()
                                pool_map_store = Store()
                                pool_map_store.bulk_extend(store.quads_for_pattern(None, None, None, mapping_graph_uri))
                                pool_map_store.bulk_extend(result_store.quads_for_pattern(None, None, None, mapping_graph_uri))

                                # for t in filter_target_subjects: # no constraining to entities alone
                                #     entries = find_entries_for_target(pool_map_store, t, mapping_graph_uri)
                                #     for entry in entries:                                        
                                #         quads = list(pool_map_store.quads_for_pattern(entry, None, None, graph_name=mapping_graph_uri))
                                #         pool_map.bulk_extend(quads)

                                                       
                                for quad in pool_map_store.quads_for_pattern(
                                    None,
                                    RDF_TYPE_NODE,
                                    safeNamedNode(MAP_ENTRY_TYPE),
                                    mapping_graph_uri,
                                ):
                                    pool_map.bulk_extend(
                                        pool_map_store.quads_for_pattern(
                                            quad.subject,
                                            None,
                                            None,
                                            graph_name=mapping_graph_uri,
                                        )
                                    )  
                                                                  
                                matched_node, score, matched_label, matched_entry = fuzzy_match_label(
                                    pool_map,
                                    lit_value,
                                    threshold=fuzzy_threshold,
                                    graph_name=mapping_graph_uri,
                                    predicates=[map_label_prop],
                                    regex=regex,
                                )                                                                   
                                if (
                                    matched_node is not None
                                    and filter_target_subjects
                                    and matched_node not in filter_target_subjects
                                ):
                                    logger.info(
                                        f"Matched mapping target {matched_node} is outside the target pool"
                                    )
                                    matched_node = None
                                    matched_entry = None

                                if matched_node is not None:
                                    result_store.add(Quad(domain_node, map_prop, matched_node, dp.graph_name))
                                    logger.debug(f"[FUZZY] Matched {lit_value} to {matched_label} ({score}%)")
                                    if matched_entry is None:
                                        entry = ensure_entry(result_store, matched_node, map_graph=mapping_graph_uri, type_hints=list(type_hints))
                                    else:
                                        entry = matched_entry
                                    ensure_mapping_literal(result_store, entry, lit_value, map_label_prop, mapping_graph_uri)                               
                                    
                                elif allow_create:
                                    entry = matched_entry

                                    # Prefer mapping entry type hints over rule-derived type hints.
                                    entry_type_hints = []

                                    if entry is not None:
                                        entry_type_hints = [
                                            q.object.value
                                            for q in pool_map.quads_for_pattern(
                                                entry,
                                                safeNamedNode(MAP_TYPE_HINT),
                                                None,
                                                mapping_graph_uri,
                                            )
                                        ]

                                    rule_type_hints = [
                                        safeNamedNode(type_hint)
                                        for type_hint in type_hints
                                    ]

                                    effective_type_hints = select_entity_types(
                                        rule_types=rule_type_hints,
                                        mapping_types=entry_type_hints,
                                        type_source=type_source,
                                    )

                                    # Prefer rdfs:label, then mapping label, then the input literal.
                                    entry_label_quad = None

                                    if entry is not None:
                                        entry_label_quad = next(
                                            pool_map.quads_for_pattern(
                                                entry,
                                                RDFS_LABEL_NODE,
                                                None,
                                                mapping_graph_uri,
                                            ),
                                            None,
                                        )

                                        if entry_label_quad is None:
                                            entry_label_quad = next(
                                                pool_map.quads_for_pattern(
                                                    entry,
                                                    map_label_prop,
                                                    None,
                                                    mapping_graph_uri,
                                                ),
                                                None,
                                            )

                                    entity_label = (
                                        entry_label_quad.object
                                        if entry_label_quad is not None
                                        else Literal(lit_value)
                                    )

                                    entity_uuid = uuid5(
                                        NAMESPACE_URL,
                                        str(entity_graph_uri.value),
                                    )

                                    iri_suffix = stable_entity_uuid(
                                        lit_value,
                                        sorted(str(type_hint) for type_hint in effective_type_hints),
                                        ENTITY_UUID=entity_uuid,
                                    )

                                    base_uri = parser_cfg.get(
                                        "base_uri",
                                        str(entity_graph_uri.value).rstrip("/"),
                                    )

                                    new_node = safeNamedNode(
                                        f"{base_uri}/{iri_suffix}"
                                    )

                                    # Apply non-type properties from the rule.
                                    for pool_rule in pool_rules:
                                        try:
                                            rule_property = safeNamedNode(
                                                pool_rule["targetProperty"]
                                            )

                                            if rule_property == NamedNode(RDF_TYPE):
                                                continue

                                            result_store.add(
                                                Quad(
                                                    new_node,
                                                    rule_property,
                                                    safeNamedNode(pool_rule["targetObject"]),
                                                    entity_graph_uri,
                                                )
                                            )
                                        except KeyError:
                                            continue

                                    # Apply mapping entry types or rule types.
                                    for type_hint in effective_type_hints:
                                        result_store.add(
                                            Quad(
                                                new_node,
                                                RDF_TYPE_NODE,
                                                safeNamedNode(type_hint),
                                                entity_graph_uri,
                                            )
                                        )

                                    # Use the mapping entry label for entity even when target_prop is rdfs:label.
                                    result_store.add(
                                        Quad(
                                            new_node,
                                            RDFS_LABEL_NODE,
                                            entity_label,
                                            entity_graph_uri,
                                        )
                                    )

                                    if target_prop not in {RDF_TYPE_NODE,RDFS_LABEL_NODE}:
                                        result_store.add(
                                            Quad(
                                                new_node,
                                                target_prop,
                                                Literal(lit_value),
                                                entity_graph_uri,
                                            )
                                        )

                                    # Adding RDF to created entity (not to the source Annotation)
                                    load_rdf_from_spec(
                                        rule,
                                        context=None,
                                        data={
                                            "value": lit_value,
                                            "label": lit_value,
                                            # "entity_id": UUID(iri_suffix.removeprefix("urn:uuid:")).int,
                                        },
                                        node_value=new_node.value,
                                        store=result_store,
                                        default_graph_uri=entity_graph_uri,
                                    )
                                    # add_timestamp(store=result_store, node=new_node,graph=entity_graph_uri)

                                    # add link from semz to entity
                                    result_store.add(
                                        Quad(
                                            domain_node,
                                            map_prop,
                                            new_node,
                                            dp.graph_name,
                                        )
                                    )

                                    # add link from mapping to entity
                                    if entry is not None:
                                        result_store.add(
                                            Quad(
                                                entry,
                                                safeNamedNode(MAP_TARGET),
                                                new_node,
                                                mapping_graph_uri,
                                            )
                                        )
                                    else: # create mapping
                                        entry = ensure_entry(
                                            result_store,
                                            new_node,
                                            map_graph=mapping_graph_uri,
                                            type_hints=effective_type_hints,
                                        )

                                    # update mapping labels
                                    ensure_mapping_literal(
                                        result_store,
                                        entry,
                                        lit_value,
                                        map_label_prop,
                                        mapping_graph_uri,
                                    )

                                    filter_target_subjects.add(new_node)

                                    if add_jsonld:                                        
                                        try:
                                            jsonld_copy = add_jsonld
                                            if "@graph" in jsonld_copy:
                                                logger.warning(f"[ADD] '@graph' found in ADD block and is ignored. Only single object additions are supported.")
                                            else:                                            
                                                jsonld_copy["@id"] = str(new_node.value)
                                                result_store.load(json.dumps(jsonld_copy), to_graph=entity_graph_uri, format=RdfFormat.JSON_LD)
                                                logger.debug(f"[ADD] Added JSON-LD supplement for {new_node}")                                             
                                        except Exception as e:
                                            logger.warning(f"[ADD] Failed to add JSON-LD for {new_node}: {e}")

                                # elif allow_create:
                                #     ENTITY_UUID = uuid5(NAMESPACE_URL, str(entity_graph_uri.value))
                                #     iri_suffix = uuid4() or uuid5(ENTITY_UUID, lit_value)
                                #     base_uri = parser_cfg.get('base_uri', f"{str(entity_graph_uri.value).rstrip('/')}") 
                                #     new_node = safeNamedNode(f"{base_uri}/{iri_suffix}") # {KB_graph}/semantic_html/{iri_suffix}

                                #     for p in pool_rules:
                                #         try:
                                #             result_store.add(Quad(
                                #                 new_node,
                                #                 safeNamedNode(p["targetProperty"]),
                                #                 safeNamedNode(p["targetObject"]),
                                #                 entity_graph_uri
                                #             ))
                                #         except KeyError:
                                #             continue

                                #     result_store.add(Quad(new_node, target_prop, Literal(lit_value), entity_graph_uri))
                                #     if target_prop != NamedNode(RDFS_LABEL):
                                #         result_store.add(Quad(new_node, NamedNode(RDFS_LABEL), Literal(lit_value), entity_graph_uri))
                                    
                                #     result_store.add(Quad(domain_node, map_prop, new_node, dp.graph_name))                                    

                                #     entry = ensure_entry(result_store, new_node, map_graph=mapping_graph_uri, type_hints=list(type_hints))
                                #     ensure_mapping_literal(result_store, entry, lit_value, map_label_prop, mapping_graph_uri)

                                #     # Update pool                                
                                #     filter_target_subjects.add(new_node)

                                #     logger.debug(f"[CREATE] New KB node for {lit_value} → {new_node}")

                            except Exception as e:
                                logger.error(f"[ERROR] Fuzzy match failed for '{lit_value}' with prop {domain_prop} → {target_prop}: {e}")

            logger.info(f"Returning parser mapping result store with {len(result_store)} quads")
            return result_store


        plugin = ParseNotePlugin(mapping=mapping, metadata=metadata)
        logger.debug("Plugin initialized")
        

        # Search notes in library graph
        if tag_filter and not query_str:
            # PREFIX zot: <{ZOT_NS}>
            # SELECT DISTINCT ?s ?p ?o WHERE {{
            # ?s zot:tags/zot:tag "{tag_filter}".
            # BIND({str(predicate) if predicate else "zot:note"} as ?p)
            # ?s ?p ?o.
            # }}
            query_str = f"""
            PREFIX zot: <{ZOT_NS}>
            SELECT DISTINCT ?s ?p ?o
            WHERE {{
            GRAPH <{GRAPH_URI.value}> {{ ?s zot:note ?o . BIND({str(predicate) if predicate else "zot:note"} AS ?p) ?s zot:tags ?t . }}
            GRAPH <{KB_GRAPH.value}>  {{ ?t zot:tag ?val . FILTER(STR(?val) = "{tag_filter}") }}
            }}
            """


        if query_str and "SELECT" in query_str.upper(): 
            if "$GRAPH" in query_str: query_str = query_str.replace("$GRAPH",GRAPH_URI.value)
            logger.info(f"using query pattern:\n{query_str}")
            bindings = store.query(query_str, use_default_graph_as_union=True)
            results = list(bindings)
            logger.info("Number of rows: %s", len(results))
            # note_quads = []
            note_items = [(row["s"], row["o"]) for row in results]
            # for row in results:  # QuerySolutions                 
                # tmp_predicate = row['p'] if 'p' in row else note_predicate            
                # quad = Quad(
                #     subject=row["s"], # the note IRI
                #     predicate=tmp_predicate,
                #     object=row["o"], # the HTML
                #     graph_name=GRAPH_URI
                # )
                # note_quads.append(quad)
        else:
            logger.info(f"using predicate pattern: {note_predicate}")
            # note_quads = list(store.quads_for_pattern(None, note_predicate, None, GRAPH_URI))
            quads = store.quads_for_pattern(None, note_predicate, None, GRAPH_URI)
            note_items = [(q.subject, q.object) for q in quads]

        logger.info("Number of notes: %s", len(note_items))

        if delete:
            logger.warning("Deleting quads!")

            # delete_query = f"""
            #     DELETE {{
            #     GRAPH <{GRAPH_URI.value}> {{
            #         ?s2 ?p ?o .
            #     }}
            #     }}
            #     WHERE {{
            #     GRAPH <{GRAPH_URI.value}> {{
            #         ?s1 <{note_predicate.value}> ?o1 .
            #         ?s2 ?p2 ?s1 .
            #         ?s2 ?p ?o .
            #     }}
            #     }}
            #     """ if note_predicate and not query_str else query_str
            # logger.info(f"Query: {delete_query}")
            try:
                # store.update(delete_query)
                if SEMANTIC_HTML_GRAPH and SEMANTIC_HTML_GRAPH != GRAPH_URI:
                    store.remove_graph(SEMANTIC_HTML_GRAPH)
                    logger.info(f"Removed named graph {SEMANTIC_HTML_GRAPH}!")
                else:
                    logger.warning(f"Did not delete graph, as identical to library graph!")
            except Exception as e:
                logger.error(f"Error when deleting triples: {e}")
        
        parser_store = Store()

        for s, o in note_items:
            subject = s
            obj = o
        # for quad in note_quads:
        #     subject = quad.subject
        #     obj = quad.object
            
            # THE ACTUAL PARSING
            if isinstance(obj, Literal):
                count += 1
                html = obj.value
                note_uri = subject.value if hasattr(subject, "value") else str(subject)
                result = plugin.run(html_str=html, note_uri=note_uri)
                logger.debug(json.dumps(result, indent=2))
                
                try:
                    tmp_store = Store()
                    tmp_store.load(json.dumps(result["JSON-LD"]), format=RdfFormat.JSON_LD, to_graph=SEMANTIC_HTML_GRAPH)
                    parser_store.extend(tmp_store)
                    # logger.debug(tmp_store.dump(format=RdfFormat.TRIG).decode("utf-8"))                    
                    logger.debug("JSON-LD parsed")
                except Exception as e:
                    logger.error(f"Error when parsing note: {e}")
        logger.info(f"Parsed {count} notes!")

        try:
            if push and count > 0:            
                store.bulk_extend(parser_store)
                logger.info(f"Extended store from parser results: {len(parser_store)} quads")
                if map_KB and knowledge_base:
                    target_store = Store()
                    target_store.bulk_extend(store.quads_for_pattern(None,None,None,KB_GRAPH))
                    logger.info(f"PUSH: Len source store ({SEMANTIC_HTML_GRAPH}): {len(parser_store)}, LEN target store ({KB_GRAPH}): {len(target_store)}")
                    logger.info(f"Extending store from mapping with {len(knowledge_base)} rules")
                    result_store = map_semantic_entities(parser_store, target_store, knowledge_base, fuzzy_threshold)
                    store.bulk_extend(result_store)
                    logger.info(f"Extended store from mapping: {len(result_store)} quads")
                else:                
                    logger.info(f"No mapping for parser provided!")
            else:
                logger.info("Serialized only")                                
                
        except Exception as e:
            logger.error(f"Error when extending store: {e}")

        # try:
        #     if parser_cfg.get('qlever_text_index'):
        #         logger.warning("Starting Qlever text index creation")
        #         from .qlever_helpers import write_qlever_text_index
        #         stats = write_qlever_text_index(
        #             store=store,
        #             config=parser_cfg.get('qlever_text_index'),
        #             load_text_like=load_text_like,
        #             base_dir=EXPORT_DIRECTORY,
        #         )

        #         logger.info(
        #             f"Wrote {stats.records} records, "
        #             f"{stats.word_occurrences} word occurrences and "
        #             f"{stats.entity_occurrences} entity occurrences"
        #         )   
        # except Exception as e:
        #     logger.error(f"Error when creating Qlever files: {e}")


    logger.info(f"Semantic-HTML parsing completed, {count} notes parsed")
    return count

# End