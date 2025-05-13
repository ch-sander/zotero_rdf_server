
from collections import defaultdict

from .logging_config import logger, setup_logging
from .config import *
from .utils import *
from .store import Quad, NamedNode, Literal, BlankNode

def zotero_schema(store, schema, vocab_iri="http://www.zotero.org/namespaces/export#"):

    
    GRAPH_URI = safeNamedNode(vocab_iri.strip("#/"))

    def uri(term): # TODO create from context dict
        if term.startswith("owl:"):
            return safeNamedNode("http://www.w3.org/2002/07/owl#" + term[4:])
        if term.startswith("rdfs:"):
            return safeNamedNode("http://www.w3.org/2000/01/rdf-schema#" + term[5:])
        if term.startswith("rdf:"):
            return safeNamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#" + term[4:])
        return safeNamedNode(vocab_iri + term)
    
    def make_rdf_list(elements):
        if not elements:
            return NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#nil")
        head = BlankNode()
        current = head
        for i, elem in enumerate(elements):
            store.add(Quad(current, uri("rdf:first"), uri(elem), graph_name=GRAPH_URI))
            next_node = BlankNode() if i < len(elements) - 1 else NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#nil")
            store.add(Quad(current, uri("rdf:rest"), next_node, graph_name=GRAPH_URI))
            current = next_node
        return head

    def add_union_triple(subject, predicate, types):
        if len(types) == 1:
            store.add(Quad(subject, uri(predicate), uri(types[0]), graph_name=GRAPH_URI))
        else:
            union_node = BlankNode()
            store.add(Quad(subject, uri(predicate), union_node, graph_name=GRAPH_URI))
            store.add(Quad(union_node, uri("rdf:type"), uri("owl:Class"), graph_name=GRAPH_URI))
            rdf_list = make_rdf_list(types)
            store.add(Quad(union_node, uri("owl:unionOf"), rdf_list, graph_name=GRAPH_URI))

    # Labels
    locales = schema.get("locales", {})
    class_labels = defaultdict(list)
    property_labels = defaultdict(list)

    for lang, content in locales.items():
        for t, label in content.get("itemTypes", {}).items():
            class_labels[t].append(Literal(label, language=lang))
        for t, label in content.get("creatorTypes", {}).items():
            class_labels[t].append(Literal(label, language=lang))
        for f, label in content.get("fields", {}).items():
            property_labels[f].append(Literal(label, language=lang))

    item_types = schema.get("itemTypes", [])
    # Create Main Classes not set in Schema
    for main_class in ["item", "library", "collection", "tag", "creatorRole"]: # TODO make dynamic
        store.add(Quad(uri(main_class), uri("rdf:type"), uri("owl:Class"), graph_name=GRAPH_URI))
        store.add(Quad(uri(main_class), uri("rdfs:label"), Literal(main_class), graph_name=GRAPH_URI))

    for item_type in item_types:
        class_name = item_type["itemType"]
        class_node = uri(class_name)
        store.add(Quad(class_node, uri("rdf:type"), uri("owl:Class"), graph_name=GRAPH_URI))
        store.add(Quad(class_node, uri("rdfs:subClassOf"), uri("item"), graph_name=GRAPH_URI)) # subclass of item
        for label in class_labels.get(class_name, []):
            store.add(Quad(class_node, uri("rdfs:label"), label, graph_name=GRAPH_URI))

    field_domains = defaultdict(set)
    base_fields = {}
    for item_type in item_types:
        class_name = item_type["itemType"]
        for field in item_type.get("fields", []):
            field_name = field["field"]
            field_domains[field_name].add(class_name)
            if "baseField" in field:
                base_fields[field_name] = field["baseField"]

    for field, domains in field_domains.items():
        prop_node = uri(field)
        store.add(Quad(prop_node, uri("rdf:type"), uri("owl:DatatypeProperty"), graph_name=GRAPH_URI))
        add_union_triple(prop_node, "rdfs:domain", list(domains))
        store.add(Quad(prop_node, uri("rdfs:range"), uri("rdfs:Literal"), graph_name=GRAPH_URI))
        for label in property_labels.get(field, []):
            store.add(Quad(prop_node, uri("rdfs:label"), label, graph_name=GRAPH_URI))
        if field in base_fields:
            store.add(Quad(prop_node, uri("owl:equivalentProperty"), uri(base_fields[field]), graph_name=GRAPH_URI))

    for item_type in item_types:
        class_name = item_type["itemType"]
        creators = item_type.get("creatorTypes", [])
        if creators:
            creator_types = [c["creatorType"] for c in creators]
            for ct in creator_types:
                ct_node = uri(ct)
                store.add(Quad(ct_node, uri("rdf:type"), uri("owl:Class"), graph_name=GRAPH_URI))
                store.add(Quad(ct_node, uri("rdfs:subClassOf"), uri("creatorRole"), graph_name=GRAPH_URI)) # subclass of item
                for label in class_labels.get(ct, []):
                    store.add(Quad(ct_node, uri("rdfs:label"), label, graph_name=GRAPH_URI))
            prop_node = uri("creators")
            store.add(Quad(prop_node, uri("rdf:type"), uri("owl:ObjectProperty"), graph_name=GRAPH_URI))
            store.add(Quad(prop_node, uri("rdfs:label"), Literal("Creators"), graph_name=GRAPH_URI))
            add_union_triple(prop_node, "rdfs:range", creator_types)
            add_union_triple(prop_node, "rdfs:domain", [class_name])