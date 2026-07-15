import { LeafletPlugin } from "./sf_bundle.js";

const G_MAPPINGS = "https://data.scigma.de/resources/mappings";
const G_ENTITIES = "https://data.scigma.de/resources/entities";

const MAP_NS = "https://zotero-rdf-server.org/mapping/";
const MAP_ENTRY = MAP_NS + "Entry";
const MAP_LABEL_P = MAP_NS + "label";
const MAP_TARGET_P = MAP_NS + "target";
const MAP_TYPEHINT_P = MAP_NS + "typeHint";

const RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";
const PROV_GENERATED_AT = "http://www.w3.org/ns/prov#generatedAtTime";
const DCTERMS_CONFORMS_TO = "http://purl.org/dc/terms/conformsTo";

const PREVIEW_PREFIXES = {
    sh: "http://www.w3.org/ns/shacl#",
    xsd: "http://www.w3.org/2001/XMLSchema#",
    rdf: "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    rdfs: "http://www.w3.org/2000/01/rdf-schema#",
    prov: "http://www.w3.org/ns/prov#",
    schema: "https://schema.org/",
    owl: "http://www.w3.org/2002/07/owl#",
    map: MAP_NS,
    dash: "http://datashapes.org/dash#",
    dcterms: "http://purl.org/dc/terms/",
    skos: "http://www.w3.org/2004/02/skos/core#",
    foaf: "http://xmlns.com/foaf/0.1/",
    org: "http://www.w3.org/ns/org#",
    wgs84: "http://www.w3.org/2003/01/geo/wgs84_pos#",
    geosparql: "http://www.opengis.net/ont/geosparql#",
    scigma: "https://data.scigma.de/resources/",
    smap: "https://data.scigma.de/resources/mappings/",
    sent: "https://data.scigma.de/resources/entities/",
    zot: "http://www.zotero.org/namespaces/export#",
    sem: "https://semantic-html.org/",
    sshap: "https://data.scigma.de/resources/shapes/"
};

const PAGE_SIZE = 25;

const FORM_PROPS = [
    "http://www.w3.org/2000/01/rdf-schema#label",
    "http://www.w3.org/2000/01/rdf-schema#comment",
    "http://www.w3.org/2002/07/owl#sameAs",
    "https://schema.org/keywords",
    RDF_TYPE,
    MAP_LABEL_P, MAP_TARGET_P, MAP_TYPEHINT_P, PROV_GENERATED_AT, DCTERMS_CONFORMS_TO,

    // place / coordinates
    "http://www.w3.org/2003/01/geo/wgs84_pos#lat",
    "http://www.w3.org/2003/01/geo/wgs84_pos#long",
    "http://www.opengis.net/ont/geosparql#hasGeometry",
    "http://www.opengis.net/ont/geosparql#asWKT",

    // foaf
    "http://xmlns.com/foaf/0.1/name",
    "http://xmlns.com/foaf/0.1/givenName",
    "http://xmlns.com/foaf/0.1/familyName",

    // skos
    "http://www.w3.org/2004/02/skos/core#prefLabel",
    "http://www.w3.org/2004/02/skos/core#altLabel",
    "http://www.w3.org/2004/02/skos/core#hiddenLabel",
    "http://www.w3.org/2004/02/skos/core#definition",
    "http://www.w3.org/2004/02/skos/core#note",
    "http://www.w3.org/2004/02/skos/core#broader",
    "http://www.w3.org/2004/02/skos/core#narrower",
    "http://www.w3.org/2004/02/skos/core#related",
    "http://www.w3.org/2004/02/skos/core#exactMatch",
    "http://www.w3.org/2004/02/skos/core#closeMatch",

    // org
    "http://www.w3.org/ns/org#identifier",
    "http://www.w3.org/ns/org#classification",
    "http://www.w3.org/ns/org#hasSite",
    "http://www.w3.org/ns/org#hasPrimarySite",
    "http://www.w3.org/ns/org#subOrganizationOf",
    "http://www.w3.org/ns/org#hasSubOrganization",
    "http://www.w3.org/ns/org#linkedTo"
];

const DB_NAME = "mapping_entity_editor_db";
const DB_VERSION = 1;
const SNAPSHOT_STORE = "snapshots";
const SNAPSHOT_KEY = "main_local_snapshot";

const SHAPES_TTL = `
@prefix sh: <http://www.w3.org/ns/shacl#>.
@prefix xsd: <http://www.w3.org/2001/XMLSchema#>.
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>.
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#>.
@prefix prov: <http://www.w3.org/ns/prov#>.
@prefix schema: <https://schema.org/>.
@prefix owl: <http://www.w3.org/2002/07/owl#>.
@prefix map: <https://zotero-rdf-server.org/mapping/>.
@prefix dash: <http://datashapes.org/dash#>.
@prefix dcterms: <http://purl.org/dc/terms/>.
@prefix skos: <http://www.w3.org/2004/02/skos/core#>.
@prefix foaf: <http://xmlns.com/foaf/0.1/>.
@prefix org: <http://www.w3.org/ns/org#>.
@prefix wgs84: <http://www.w3.org/2003/01/geo/wgs84_pos#>.
@prefix geo: <http://www.opengis.net/ont/geosparql#>.
@prefix scigma: <https://data.scigma.de/resources/>.
@prefix smap: <https://data.scigma.de/resources/mappings/>.
@prefix sent: <https://data.scigma.de/resources/entities/>.
@prefix zot: <http://www.zotero.org/namespaces/export#>.
@prefix sem: <https://semantic-html.org/>.
@prefix sshap: <https://data.scigma.de/resources/shapes/>.

#################################################################
# Property groups
#################################################################

sshap:MappingGroup
a sh:PropertyGroup ;
rdfs:label "Mapping"@en, "Mapping"@de ;
sh:order 1 .

sshap:CommonEntityGroup
a sh:PropertyGroup ;
rdfs:label "Common"@en, "Allgemein"@de ;
sh:order 1 .

sshap:PlaceGroup
a sh:PropertyGroup ;
rdfs:label "Place"@en, "Ort"@de ;
sh:order 2 .

sshap:AgentGroup
a sh:PropertyGroup ;
rdfs:label "Actor / Agent"@en, "Akteur / Agent"@de ;
sh:order 3 .

sshap:SkosGroup
a sh:PropertyGroup ;
rdfs:label "SKOS Concept"@en, "SKOS-Konzept"@de ;
sh:order 4 .

sshap:OrgGroup
a sh:PropertyGroup ;
rdfs:label "Publisher / Organization"@en, "Publisher / Organisation"@de ;
sh:order 5 .

#################################################################
# Mapping properties
#################################################################

sshap:MappingLabelProperty
a sh:PropertyShape ;
sh:path <${MAP_LABEL_P}> ;
sh:name "Mapping label"@en, "Mapping-Bezeichnung"@de ;
sh:datatype xsd:string ;
sh:minCount 1 ;
sh:group sshap:MappingGroup ;
sh:order 1 .

sshap:MappingTypeHintProperty
a sh:PropertyShape ;
sh:path <${MAP_TYPEHINT_P}> ;
sh:name "Type hint"@en, "Typ-Hinweis"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:MappingGroup ;
sh:order 2 .

sshap:MappingTargetProperty
a sh:PropertyShape ;
sh:path <${MAP_TARGET_P}> ;
sh:name "Target entity (IRI)"@en, "Ziel-Entität (IRI)"@de ;
sh:nodeKind sh:IRI ;
sh:pattern "^https?://.*" ;
sh:minCount 1 ;
sh:maxCount 1 ;
sh:message "Target must be an IRI!"@en, "Target muss eine IRI sein!"@de ;
sh:group sshap:MappingGroup ;
sh:order 4 .

sshap:MappingGeneratedAtProperty
a sh:PropertyShape ;
sh:path prov:generatedAtTime ;
sh:name "Generated at"@en, "Erzeugt am"@de ;
sh:datatype xsd:dateTime ;
sh:maxCount 1 ;
sh:group sshap:MappingGroup ;
sh:order 3 .

#################################################################
# Common entity properties
#################################################################

sshap:EntityLabelProperty
a sh:PropertyShape ;
sh:path rdfs:label ;
sh:name "Label"@en, "Bezeichnung"@de ;
sh:datatype xsd:string ;
sh:group sshap:CommonEntityGroup ;
sh:order 1 .

sshap:EntityTypeProperty
a sh:PropertyShape ;
sh:path rdf:type ;
sh:name "Types"@en, "Typen"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:CommonEntityGroup ;
sh:order 2 .

sshap:EntityGeneratedAtProperty
a sh:PropertyShape ;
sh:path prov:generatedAtTime ;
sh:name "Generated at"@en, "Erzeugt am"@de ;
sh:datatype xsd:dateTime ;
sh:maxCount 1 ;
sh:group sshap:CommonEntityGroup ;
sh:order 3 .

sshap:EntityCommentProperty
a sh:PropertyShape ;
sh:path rdfs:comment ;
sh:name "Comment"@en, "Kommentar"@de ;
sh:description "Add your comment"@en, "Kommentar hinzufügen"@de ;
sh:datatype rdf:langString ;
sh:languageIn ( "en" "de" "la") ;
sh:maxCount 1 ;
dash:singleLine false ;
sh:group sshap:CommonEntityGroup ;
sh:order 4 .

sshap:EntitySameAsProperty
a sh:PropertyShape ;
sh:path owl:sameAs ;
sh:name "sameAs"@en, "Gleich wie"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:CommonEntityGroup ;
sh:order 5 .

sshap:EntityKeywordsProperty
a sh:PropertyShape ;
sh:path schema:keywords ;
sh:name "Keywords"@en, "Schlagwörter"@de ;
sh:datatype xsd:string ;
sh:group sshap:CommonEntityGroup ;
sh:order 6 .

#################################################################
# Place properties for zot:Place
#################################################################

sshap:PlaceLatitudeProperty
a sh:PropertyShape ;
sh:path wgs84:lat ;
sh:name "Latitude"@en, "Breitengrad"@de ;
sh:datatype xsd:decimal ;
sh:minInclusive -90 ;
sh:maxInclusive 90 ;
sh:maxCount 1 ;
sh:group sshap:PlaceGroup ;
sh:order 20 .

sshap:PlaceLongitudeProperty
a sh:PropertyShape ;
sh:path wgs84:long ;
sh:name "Longitude"@en, "Längengrad"@de ;
sh:datatype xsd:decimal ;
sh:minInclusive -180 ;
sh:maxInclusive 180 ;
sh:maxCount 1 ;
sh:group sshap:PlaceGroup ;
sh:order 21 .

sshap:PlaceWktProperty
a sh:PropertyShape ;
sh:path geo:asWKT ;
sh:name "WKT geometry"@en, "WKT-Geometrie"@de ;
sh:datatype geo:wktLiteral ;
dash:singleLine false ;
sh:group sshap:PlaceGroup ;
sh:order 22 .

#################################################################
# Place geometry as blank node
#################################################################

sshap:PlaceGeometryProperty
a sh:PropertyShape ;
sh:path geo:hasGeometry ;
sh:name "Geometry"@en, "Geometrie"@de ;
sh:nodeKind sh:BlankNode ;
sh:node sshap:GeometryShape ;
sh:minCount 1 ;
sh:maxCount 1 ;
sh:group sshap:PlaceGroup ;
sh:order 22 .

sshap:GeometryShape
a sh:NodeShape ;
sh:targetClass geo:Geometry ;
sh:property sshap:GeometryWktProperty .


sshap:GeometryWktProperty
a sh:PropertyShape ;
sh:path geo:asWKT ;
sh:name "WKT geometry"@en, "WKT-Geometrie"@de ;
sh:datatype geo:wktLiteral ;
sh:minCount 1 ;
sh:maxCount 1 ;
dash:singleLine false ;
sh:order 1 .

#################################################################
# FOAF properties for zot:Agent and foaf:Agent
#################################################################

sshap:FoafNameProperty
a sh:PropertyShape ;
sh:path foaf:name ;
sh:name "Name"@en, "Name"@de ;
sh:datatype xsd:string ;
sh:group sshap:AgentGroup ;
sh:order 30 .

sshap:FoafGivenNameProperty
a sh:PropertyShape ;
sh:path foaf:givenName ;
sh:name "Given name"@en, "Vorname"@de ;
sh:datatype xsd:string ;
sh:group sshap:AgentGroup ;
sh:order 31 .

sshap:FoafFamilyNameProperty
a sh:PropertyShape ;
sh:path foaf:familyName ;
sh:name "Family name"@en, "Nachname"@de ;
sh:datatype xsd:string ;
sh:group sshap:AgentGroup ;
sh:order 32 .

#################################################################
# SKOS properties for skos:Concept
#################################################################

sshap:SkosPrefLabelProperty
a sh:PropertyShape ;
sh:path skos:prefLabel ;
sh:name "Preferred label"@en, "Bevorzugte Bezeichnung"@de ;
sh:datatype rdf:langString ;
sh:languageIn ( "en" "de" "la") ;
sh:group sshap:SkosGroup ;
sh:order 40 .

sshap:SkosAltLabelProperty
a sh:PropertyShape ;
sh:path skos:altLabel ;
sh:name "Alternative label"@en, "Alternative Bezeichnung"@de ;
sh:datatype rdf:langString ;
sh:languageIn ( "en" "de" "la") ;
sh:group sshap:SkosGroup ;
sh:order 41 .

sshap:SkosHiddenLabelProperty
a sh:PropertyShape ;
sh:path skos:hiddenLabel ;
sh:name "Hidden label"@en, "Versteckte Bezeichnung"@de ;
sh:datatype rdf:langString ;
sh:languageIn ( "en" "de" "la") ;
sh:group sshap:SkosGroup ;
sh:order 42 .

sshap:SkosDefinitionProperty
a sh:PropertyShape ;
sh:path skos:definition ;
sh:name "Definition"@en, "Definition"@de ;
sh:datatype rdf:langString ;
sh:languageIn ( "en" "de" "la") ;
dash:singleLine false ;
sh:group sshap:SkosGroup ;
sh:order 43 .

sshap:SkosNoteProperty
a sh:PropertyShape ;
sh:path skos:note ;
sh:name "Note"@en, "Notiz"@de ;
sh:datatype rdf:langString ;
sh:languageIn ( "en" "de" "la") ;
dash:singleLine false ;
sh:group sshap:SkosGroup ;
sh:order 44 .

sshap:SkosScopeNoteProperty
a sh:PropertyShape ;
sh:path skos:scopeNote ;
sh:name "Scope note"@en, "Geltungshinweis"@de ;
sh:datatype rdf:langString ;
sh:languageIn ( "en" "de" "la") ;
dash:singleLine false ;
sh:group sshap:SkosGroup ;
sh:order 45 .

sshap:SkosBroaderProperty
a sh:PropertyShape ;
sh:path skos:broader ;
sh:name "Broader concept"@en, "Oberbegriff"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:SkosGroup ;
sh:order 46 .

sshap:SkosNarrowerProperty
a sh:PropertyShape ;
sh:path skos:narrower ;
sh:name "Narrower concept"@en, "Unterbegriff"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:SkosGroup ;
sh:order 47 .

sshap:SkosRelatedProperty
a sh:PropertyShape ;
sh:path skos:related ;
sh:name "Related concept"@en, "Verwandtes Konzept"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:SkosGroup ;
sh:order 48 .

sshap:SkosInSchemeProperty
a sh:PropertyShape ;
sh:path skos:inScheme ;
sh:name "Concept scheme"@en, "Konzeptschema"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:SkosGroup ;
sh:order 49 .

sshap:SkosExactMatchProperty
a sh:PropertyShape ;
sh:path skos:exactMatch ;
sh:name "Exact match"@en, "Exakte Entsprechung"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:SkosGroup ;
sh:order 50 .

sshap:SkosCloseMatchProperty
a sh:PropertyShape ;
sh:path skos:closeMatch ;
sh:name "Close match"@en, "Nahe Entsprechung"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:SkosGroup ;
sh:order 51 .

#################################################################
# ORG properties for zot:Publisher
#################################################################

sshap:OrgIdentifierProperty
a sh:PropertyShape ;
sh:path org:identifier ;
sh:name "Organization identifier"@en, "Organisations-ID"@de ;
sh:datatype xsd:string ;
sh:group sshap:OrgGroup ;
sh:order 60 .

sshap:OrgClassificationProperty
a sh:PropertyShape ;
sh:path org:classification ;
sh:name "Organization classification"@en, "Organisationsklassifikation"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:OrgGroup ;
sh:order 61 .

sshap:OrgHasSiteProperty
a sh:PropertyShape ;
sh:path org:hasSite ;
sh:name "Site"@en, "Standort"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:OrgGroup ;
sh:order 62 .

sshap:OrgHasPrimarySiteProperty
a sh:PropertyShape ;
sh:path org:hasPrimarySite ;
sh:name "Primary site"@en, "Hauptstandort"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:OrgGroup ;
sh:order 63 .

sshap:OrgSubOrganizationOfProperty
a sh:PropertyShape ;
sh:path org:subOrganizationOf ;
sh:name "Sub-organization of"@en, "Unterorganisation von"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:OrgGroup ;
sh:order 64 .

sshap:OrgHasSubOrganizationProperty
a sh:PropertyShape ;
sh:path org:hasSubOrganization ;
sh:name "Has sub-organization"@en, "Hat Unterorganisation"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:OrgGroup ;
sh:order 65 .

sshap:OrgLinkedToProperty
a sh:PropertyShape ;
sh:path org:linkedTo ;
sh:name "Linked organization"@en, "Verknüpfte Organisation"@de ;
sh:nodeKind sh:IRI ;
sh:group sshap:OrgGroup ;
sh:order 66 .

#################################################################
# NodeShapes with sh:targetClass
#################################################################

sshap:MappingShape
a sh:NodeShape ;
sh:targetClass <${MAP_ENTRY}> ;
sh:property
sshap:MappingLabelProperty,
sshap:MappingTypeHintProperty,
sshap:MappingGeneratedAtProperty,
sshap:MappingTargetProperty .

sshap:GenericEntityShape
a sh:NodeShape ;
sh:targetClass owl:Thing ;
sh:property
sshap:EntityLabelProperty,
sshap:EntityTypeProperty,
sshap:EntityGeneratedAtProperty,
sshap:EntityCommentProperty,
sshap:EntitySameAsProperty,
sshap:EntityKeywordsProperty .

sshap:PlaceShape
a sh:NodeShape ;
sh:targetClass zot:Place ;
sh:property
sshap:EntityLabelProperty,
sshap:EntityTypeProperty,
sshap:EntityGeneratedAtProperty,
sshap:EntityCommentProperty,
sshap:EntitySameAsProperty,
sshap:EntityKeywordsProperty,
# sshap:PlaceWktProperty,
sshap:PlaceLatitudeProperty,
sshap:PlaceLongitudeProperty,
sshap:PlaceGeometryProperty .


sshap:ActorShape
a sh:NodeShape ;
sh:targetClass zot:Agent ;
sh:property
sshap:EntityLabelProperty,
sshap:EntityTypeProperty,
sshap:EntityGeneratedAtProperty,
sshap:EntityCommentProperty,
sshap:EntitySameAsProperty,
sshap:EntityKeywordsProperty,
sshap:FoafNameProperty,
sshap:FoafGivenNameProperty,
sshap:FoafFamilyNameProperty .

sshap:AgentShape
a sh:NodeShape ;
sh:targetClass foaf:Agent ;
sh:property
sshap:EntityLabelProperty,
sshap:EntityTypeProperty,
sshap:EntityGeneratedAtProperty,
sshap:EntityCommentProperty,
sshap:EntitySameAsProperty,
sshap:EntityKeywordsProperty,
sshap:FoafNameProperty,
sshap:FoafGivenNameProperty,
sshap:FoafFamilyNameProperty .

sshap:ConceptShape
a sh:NodeShape ;
sh:targetClass skos:Concept ;
sh:property
sshap:EntityLabelProperty,
sshap:EntityTypeProperty,
sshap:EntityGeneratedAtProperty,
sshap:EntityCommentProperty,
sshap:EntitySameAsProperty,
sshap:EntityKeywordsProperty,
sshap:SkosPrefLabelProperty,
sshap:SkosAltLabelProperty,
sshap:SkosHiddenLabelProperty,
sshap:SkosDefinitionProperty,
sshap:SkosNoteProperty,
sshap:SkosScopeNoteProperty,
sshap:SkosBroaderProperty,
sshap:SkosNarrowerProperty,
sshap:SkosRelatedProperty,
sshap:SkosInSchemeProperty,
sshap:SkosExactMatchProperty,
sshap:SkosCloseMatchProperty .

sshap:PublisherShape
a sh:NodeShape ;
sh:targetClass zot:Publisher ;
sh:property
sshap:EntityLabelProperty,
sshap:EntityTypeProperty,
sshap:EntityGeneratedAtProperty,
sshap:EntityCommentProperty,
sshap:EntitySameAsProperty,
sshap:EntityKeywordsProperty,
sshap:OrgIdentifierProperty,
sshap:OrgClassificationProperty,
sshap:OrgHasSiteProperty,
sshap:OrgHasPrimarySiteProperty,
sshap:OrgSubOrganizationOfProperty,
sshap:OrgHasSubOrganizationProperty,
sshap:OrgLinkedToProperty .
`.trim();

const $ = (id) => document.getElementById(id);

const openFileBtn = $("openFile");
const clearLocalBtn = $("clearLocal");
const fileInput = $("fileInput");
const fileInfo = $("fileInfo");

const endpointEl = $("endpoint");
const loadRemoteBtn = $("loadRemote");
const clearEndpointBtn = $("clearEndpoint");
const unionDefaultGraphEl = $("unionDefaultGraph");
const sourceInfoEl = $("sourceInfo");

const listEl = $("list");
const qEl = $("q");
const typeHintFilterEl = $("typeHintFilter");
const pageEl = $("page");
const statusEl = $("status");

const prevBtn = $("prev");
const nextBtn = $("next");
const reloadBtn = $("reload");

const exportSelectedBtn = $("exportSelected");
const exportMappingBtn = $("exportMapping");
const exportEntityBtn = $("exportEntity");
const exportFullBtn = $("exportFull");
const saveBtn = $("saveSelected");

const showSelectedBtn = $("showSelected");
const showMappingBtn = $("showMapping");
const showEntityBtn = $("showEntity");
const copyTrigBtn = $("copyTrig");
const clearTrigBtn = $("clearTrig");
const trigOut = $("trigOut");

const newMappingBtn = $("newMapping");
const deleteMappingBtn = $("deleteMapping");
const newEntityBtn = $("newEntity");
const deleteEntityBtn = $("deleteEntity");

const mappingIriEl = $("mappingIri");
const entityIriEl = $("entityIri");
const copyMappingIriEl = $("copyMappingIri");
const copyEntityIriEl = $("copyEntityIri");
const mappingForm = $("mappingForm");
const entityForm = $("entityForm");
await customElements.whenDefined("shacl-form");

if (!entityForm) {
    throw new Error("#entityForm not found.");
}

entityForm.registerPlugin(
    new LeafletPlugin({
        datatype: "http://www.opengis.net/ont/geosparql#wktLiteral"
    })
);
let page = 0;
let selectedMapping = null;
let selectedTarget = null;
let previewTimer = null;
let syncTimer = null;
let remoteEndpoint = "";
let loadedRemoteMeta = null;

const dataset = new window.N3.Store();
const storeMappings = new window.N3.Store();
const storeEntities = new window.N3.Store();
const savedTTL = { mappings: new Map(), entities: new Map() };
const engine = new window.Comunica.QueryEngine();

function setStatus(msg) { statusEl.textContent = msg || ""; }
function escapeHtml(s) {
    return String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}
function subjectTerm(iri) { return window.N3.DataFactory.namedNode(iri); }
function graphTerm(iri) { return window.N3.DataFactory.namedNode(iri); }

function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes < 0) return "unknown size";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function setListLoading(isLoading, msg = "Loading…") {
    prevBtn.disabled = isLoading || page === 0;
    nextBtn.disabled = isLoading;
    reloadBtn.disabled = isLoading;
    if (isLoading) {
        listEl.innerHTML = `<div class="muted"><span class="spinner"></span>${escapeHtml(msg)}</div>`;
    }
}

function refreshActions() {
    exportMappingBtn.disabled = !selectedMapping;
    exportEntityBtn.disabled = !selectedTarget;
    exportSelectedBtn.disabled = !(selectedMapping && selectedTarget);
    showMappingBtn.disabled = !selectedMapping;
    showEntityBtn.disabled = !selectedTarget;
    showSelectedBtn.disabled = !(selectedMapping || selectedTarget);
    deleteMappingBtn.disabled = !selectedMapping;
    deleteEntityBtn.disabled = !selectedTarget;
    saveBtn.disabled = !(selectedMapping || selectedTarget);
    copyTrigBtn.disabled = !(trigOut.value && trigOut.value.trim().length);
}

function updateIriHeadersAndCopyState() {
    mappingIriEl.textContent = selectedMapping || "(none)";
    entityIriEl.textContent = selectedTarget || "(none)";
    copyMappingIriEl.classList.toggle("disabled", !selectedMapping);
    copyEntityIriEl.classList.toggle("disabled", !selectedTarget);
}

function updateSourceInfo() {
    const localCount = dataset.size || 0;
    const endpointTxt = remoteEndpoint ? `endpoint: ${remoteEndpoint}` : "endpoint: (none)";
    const remoteTxt = loadedRemoteMeta ? `remote snapshot loaded: ${loadedRemoteMeta}` : "remote snapshot loaded: no";
    sourceInfoEl.textContent = `Store: local (${localCount} quads) • ${endpointTxt} • ${remoteTxt}`;
}

function openSnapshotDb() {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = () => {
            const db = req.result;
            if (!db.objectStoreNames.contains(SNAPSHOT_STORE)) {
                db.createObjectStore(SNAPSHOT_STORE);
            }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
}

async function datasetToNQuads() {
    const writer = new window.N3.Writer({ format: "application/n-quads" });
    for (const q of dataset.getQuads(null, null, null, null)) writer.addQuad(q);
    return await new Promise((resolve, reject) => writer.end((e, out) => e ? reject(e) : resolve(out)));
}

async function saveSnapshotToIndexedDb() {
    const db = await openSnapshotDb();
    const nquads = await datasetToNQuads();
    const payload = {
        nquads,
        savedAt: new Date().toISOString(),
        remoteEndpoint,
        loadedRemoteMeta,
    };
    await new Promise((resolve, reject) => {
        const tx = db.transaction(SNAPSHOT_STORE, "readwrite");
        tx.objectStore(SNAPSHOT_STORE).put(payload, SNAPSHOT_KEY);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
    });
    db.close();
}

async function loadSnapshotFromIndexedDb() {
    const db = await openSnapshotDb();
    const payload = await new Promise((resolve, reject) => {
        const tx = db.transaction(SNAPSHOT_STORE, "readonly");
        const req = tx.objectStore(SNAPSHOT_STORE).get(SNAPSHOT_KEY);
        req.onsuccess = () => resolve(req.result || null);
        req.onerror = () => reject(req.error);
    });
    db.close();

    if (!payload || !payload.nquads) return false;

    const parser = new window.N3.Parser({ format: "application/n-quads" });
    const quads = parser.parse(payload.nquads);

    clearLocalStores();
    addQuadsToStores(quads);
    rebuildPerGraphStores();

    remoteEndpoint = payload.remoteEndpoint || "";
    loadedRemoteMeta = payload.loadedRemoteMeta || `restored ${payload.savedAt || ""}`;
    if (remoteEndpoint) endpointEl.value = remoteEndpoint;

    updateSourceInfo();
    return true;
}

async function clearSnapshotFromIndexedDb() {
    const db = await openSnapshotDb();
    await new Promise((resolve, reject) => {
        const tx = db.transaction(SNAPSHOT_STORE, "readwrite");
        tx.objectStore(SNAPSHOT_STORE).delete(SNAPSHOT_KEY);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
    });
    db.close();
}

function mintIRI(base) { return base + "/" + crypto.randomUUID(); }

async function copyText(text) {
    const s = (text || "").trim();
    if (!s) return false;
    try {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(s);
            return true;
        }
    } catch { }
    try {
        const ta = document.createElement("textarea");
        ta.value = s;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        const ok = document.execCommand("copy");
        document.body.removeChild(ta);
        return ok;
    } catch {
        return false;
    }
}

async function handleCopy(what) {
    const s = (what || "").trim();
    if (!s) { setStatus("Nothing to copy."); setTimeout(() => setStatus(""), 800); return; }
    const ok = await copyText(s);
    setStatus(ok ? "Copied." : "Copy failed.");
    setTimeout(() => setStatus(""), 800);
    if (!ok) window.prompt("Copy:", s);
}

function wireCopyOnce(el, getter) {
    const run = async () => {
        if (el.classList.contains("disabled")) return;
        await handleCopy(getter());
    };
    el.addEventListener("click", run);
    el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); run(); }
    });
}

function localSource() {
    return { type: "rdfjsSource", value: dataset };
}

function bindingsStreamToArray(stream) {
    return new Promise((resolve, reject) => {
        const out = [];
        stream.on("data", (b) => out.push(b));
        stream.on("end", () => resolve(out));
        stream.on("error", reject);
    });
}

function termToSparqlJson(term) {
    if (!term) return null;
    if (term.termType === "NamedNode") return { type: "uri", value: term.value };
    if (term.termType === "BlankNode") return { type: "bnode", value: term.value };
    if (term.termType === "Literal") {
        const obj = { type: "literal", value: term.value };
        if (term.language) obj["xml:lang"] = term.language;
        if (term.datatype?.value) obj.datatype = term.datatype.value;
        return obj;
    }
    return { type: "unknown", value: String(term.value || "") };
}

async function sparqlSelectLocal(query) {
    const bs = await engine.queryBindings(query, {
        sources: [localSource()],
        unionDefaultGraph: !!unionDefaultGraphEl.checked,
    });
    const bindingsArr = await bindingsStreamToArray(bs);
    const varsSet = new Set();
    const rows = [];
    for (const b of bindingsArr) {
        const row = {};
        for (const [key, value] of b) {
            const varName = typeof key === "string"
                ? key.replace(/^\?/, "")
                : (key && typeof key.value === "string" ? key.value : String(key));
            varsSet.add(varName);
            row[varName] = termToSparqlJson(value);
        }
        rows.push(row);
    }
    return { head: { vars: Array.from(varsSet) }, results: { bindings: rows } };
}

function initForms_old() {
    mappingForm.setAttribute("data-shapes", SHAPES_TTL);
    mappingForm.setAttribute("data-shape-subject", "https://data.scigma.de/resources/shapes/MappingShape");
    entityForm.setAttribute("data-shapes", SHAPES_TTL);
    entityForm.setAttribute("data-shape-subject", "https://data.scigma.de/resources/shapes/EntityShape");
}

function initForms() {
    mappingForm.setAttribute("data-shapes", SHAPES_TTL);
    mappingForm.setAttribute("data-shape-subject", "https://data.scigma.de/resources/shapes/MappingShape");

    entityForm.setAttribute("data-shapes", SHAPES_TTL);
    entityForm.removeAttribute("data-shape-subject");
}

function guessFormat(text, fileName = "") {
    const lower = (fileName || "").toLowerCase();
    if (lower.endsWith(".trig")) return "application/trig";
    if (lower.endsWith(".ttl")) return "text/turtle";
    if (lower.endsWith(".nq")) return "application/n-quads";
    if (lower.endsWith(".nt")) return "application/n-triples";
    if (text.includes("{") && text.includes("}")) return "application/trig";
    return "text/turtle";
}

function parseToQuads(text, format) {
    const baseIRI = document.baseURI || window.location.href;
    const parser = new window.N3.Parser({ format, baseIRI });
    return parser.parse(text);
}

function clearSelection() {
    selectedMapping = null;
    selectedTarget = null;
    mappingForm.setAttribute("data-values", "");
    mappingForm.removeAttribute("data-values-subject");
    entityForm.setAttribute("data-values", "");
    entityForm.removeAttribute("data-values-subject");
    trigOut.value = "";
    updateIriHeadersAndCopyState();
    refreshActions();
}

function clearLocalStores() {
    dataset.removeQuads(dataset.getQuads(null, null, null, null));
    storeMappings.removeQuads(storeMappings.getQuads(null, null, null, null));
    storeEntities.removeQuads(storeEntities.getQuads(null, null, null, null));
    savedTTL.mappings.clear();
    savedTTL.entities.clear();
    loadedRemoteMeta = null;
    updateSourceInfo();
}

function rebuildPerGraphStores() {
    storeMappings.removeQuads(storeMappings.getQuads(null, null, null, null));
    storeEntities.removeQuads(storeEntities.getQuads(null, null, null, null));
    const gM = graphTerm(G_MAPPINGS);
    const gE = graphTerm(G_ENTITIES);
    for (const q of dataset.getQuads(null, null, null, null)) {
        if (q.graph?.termType !== "NamedNode") continue;
        if (q.graph.equals(gM)) storeMappings.addQuad(q);
        else if (q.graph.equals(gE)) storeEntities.addQuad(q);
    }
}

function addQuadsToStores(quads) {
    dataset.addQuads(quads);
    const gM = graphTerm(G_MAPPINGS);
    const gE = graphTerm(G_ENTITIES);
    for (const q of quads) {
        if (q.graph?.termType !== "NamedNode") continue;
        if (q.graph.equals(gM)) storeMappings.addQuad(q);
        else if (q.graph.equals(gE)) storeEntities.addQuad(q);
    }
}

async function loadFileTextIntoStores(text, fileName) {
    setStatus("Parsing local file…");
    let quads;
    try {
        quads = parseToQuads(text, guessFormat(text, fileName));
    } catch {
        quads = parseToQuads(text, "application/trig");
    }
    addQuadsToStores(quads);
    updateSourceInfo();
    await saveSnapshotToIndexedDb();
    setStatus("");
}

async function importRemoteGraphsIntoLocal_Comunica() {
    const url = (endpointEl.value || "").trim();
    if (!url) throw new Error("Remote endpoint missing.");
    remoteEndpoint = url;
    clearLocalStores();
    const DF = window.N3.DataFactory;
    const graphs = [G_MAPPINGS, G_ENTITIES];
    const imported = [];

    setStatus("Loading remote into local store…");

    for (const graphIri of graphs) {
        const g = DF.namedNode(graphIri);
        const query = `
    CONSTRUCT { ?s ?p ?o }
    WHERE     { GRAPH <${graphIri}> { ?s ?p ?o } }
    `.trim();

        const stream = await engine.queryQuads(query, {
            sources: [{ type: "sparql", value: remoteEndpoint }],
            unionDefaultGraph: !!unionDefaultGraphEl.checked,
        });

        await new Promise((resolve, reject) => {
            stream.on("data", (q) => imported.push(DF.quad(q.subject, q.predicate, q.object, g)));
            stream.on("error", reject);
            stream.on("end", resolve);
        });
    }

    addQuadsToStores(imported);
    loadedRemoteMeta = `${new Date().toLocaleString()} (${imported.length} quads)`;
    updateSourceInfo();
    await saveSnapshotToIndexedDb();
    setStatus("Remote snapshot imported.");
    setTimeout(() => setStatus(""), 1000);
}


async function fetchConstructGraph(graphIri) {
    const DF = window.N3.DataFactory;

    const query = `
CONSTRUCT { ?s ?p ?o }
WHERE { GRAPH <${graphIri}> { ?s ?p ?o } }
`.trim();

    const res = await fetch(remoteEndpoint, {
        method: "POST",
        headers: {
            "Accept": "application/n-triples,text/turtle;q=0.9,application/rdf+xml;q=0.1",
            "Content-Type": "application/sparql-query"
        },
        body: query
    });

    if (!res.ok) {
        throw new Error(`SPARQL import failed: ${res.status} ${res.statusText}`);
    }

    const text = await res.text();
    const ct = res.headers.get("content-type") || "";

    const format = ct.includes("turtle")
        ? "text/turtle"
        : "application/n-triples";

    const parser = new window.N3.Parser({
        format,
        baseIRI: document.baseURI || window.location.href
    });

    const g = DF.namedNode(graphIri);
    return parser.parse(text).map(q =>
        DF.quad(q.subject, q.predicate, q.object, g)
    );
}

async function importRemoteGraphsIntoLocal() {
    const url = (endpointEl.value || "").trim();
    if (!url) throw new Error("Remote endpoint missing.");
    remoteEndpoint = url;

    const graphs = [G_MAPPINGS, G_ENTITIES];

    setStatus("Loading remote RDF dump…");

    const graphQuads = await Promise.all(
        graphs.map(g => fetchConstructGraph(g))
    );

    const imported = graphQuads.flat();

    addQuadsToStores(imported);

    loadedRemoteMeta = `${new Date().toLocaleString()} (${imported.length} quads)`;
    updateSourceInfo();

    setStatus("Remote snapshot imported.");

    await saveSnapshotToIndexedDb();

    setTimeout(() => setStatus(""), 1000);
}

async function loadTypeHints() {
    const query = `
SELECT DISTINCT ?t WHERE {
GRAPH <${G_MAPPINGS}> { ?m <${MAP_TYPEHINT_P}> ?t . }
}
ORDER BY STR(?t)
LIMIT 500
`.trim();
    try {
        const data = await sparqlSelectLocal(query);
        const hints = data.results.bindings.map(b => b.t?.value).filter(Boolean);
        const current = typeHintFilterEl.value;
        typeHintFilterEl.innerHTML = `<option value="">All type hints</option>`;
        for (const t of hints) {
            const opt = document.createElement("option");
            opt.value = t;
            opt.textContent = t;
            typeHintFilterEl.appendChild(opt);
        }
        if (hints.includes(current)) typeHintFilterEl.value = current;
    } catch (e) {
        console.warn("Could not load type hints:", e);
    }
}

async function loadMappingList() {
    setStatus("Loading mappings…");
    setListLoading(true, `Loading page ${page + 1}…`);
    try {
        const q = (qEl.value || "").trim().toLowerCase();
        const offset = page * PAGE_SIZE;
        const hintFilter = (typeHintFilterEl.value || "").trim();

        const query = `
    SELECT ?m
        (SAMPLE(?label) AS ?label)
        (SAMPLE(?time) AS ?time)
        (SAMPLE(?hint) AS ?hint)
        (SAMPLE(?target) AS ?target)
    WHERE {
    GRAPH <${G_MAPPINGS}> {
        ?m <${MAP_LABEL_P}> ?label .
        OPTIONAL { ?m <${PROV_GENERATED_AT}> ?time . }
        OPTIONAL { ?m <${MAP_TYPEHINT_P}> ?hint . }
        OPTIONAL { ?m <${MAP_TARGET_P}> ?target . }
        ${q ? `FILTER(CONTAINS(LCASE(STR(?label)), "${q.replaceAll('"', '\\"')}"))` : ""}
    }
    ${hintFilter ? `FILTER(EXISTS { GRAPH <${G_MAPPINGS}> { ?m <${MAP_TYPEHINT_P}> <${hintFilter}> } })` : ""}
    }
    GROUP BY ?m
    ORDER BY LCASE(STR(?label)) STR(?m)
    LIMIT ${PAGE_SIZE}
    OFFSET ${offset}
    `.trim();

        const data = await sparqlSelectLocal(query);
        const rows = data.results.bindings;
        listEl.innerHTML = "";

        if (!rows.length) {
            listEl.innerHTML = `<div class="muted">No results.</div>`;
            setStatus("");
            return;
        }

        for (const b of rows) {
            const iri = b.m?.value || "";
            const label = b.label?.value || "(no label)";
            const time = b.time?.value || "";
            const hint = b.hint?.value || "";
            const target = b.target?.value || "";
            const needsTarget = !target;
            const isRemoteImported = !savedTTL.mappings.has(iri) && !!loadedRemoteMeta;

            const div = document.createElement("div");
            div.className =
                "item" +
                (iri === selectedMapping ? " active" : "") +
                (needsTarget ? " needsTarget" : "");

            div.innerHTML = `
        <div>
        <strong>${escapeHtml(label)}</strong>
        ${needsTarget ? `<span class="badge warn">missing target</span>` : ""}
        ${isRemoteImported ? `<span class="badge info">snapshot</span>` : ""}
        </div>
        <div class="muted">${escapeHtml(iri)}</div>
        ${hint ? `<div class="muted">typeHint: ${escapeHtml(hint)}</div>` : `<div class="muted">(no typeHint)</div>`}
        ${time ? `<div class="muted">time: ${escapeHtml(time)}</div>` : ""}
    `;
            div.addEventListener("click", () => selectMapping(iri));
            listEl.appendChild(div);
        }

        setStatus("");
    } finally {
        setListLoading(false);
    }
}

async function fetchTargetIri(mappingIri) {
    const q = `
SELECT ?target WHERE {
GRAPH <${G_MAPPINGS}> { <${mappingIri}> <${MAP_TARGET_P}> ?target . }
} LIMIT 1
`.trim();
    const data = await sparqlSelectLocal(q);
    return data.results.bindings[0]?.target?.value || null;
}

function rdfTermKey(term) {
    return `${term.termType}:${term.value}`;
}

function collectBlankNodeClosureFromStore(
    store,
    graphIri,
    blankNodeSeeds
) {
    const g = graphTerm(graphIri);
    const visited = new Set();
    const stack = [...blankNodeSeeds];
    const quads = [];

    while (stack.length) {
        const current = stack.pop();

        if (!current || current.termType !== "BlankNode") {
            continue;
        }

        const key = rdfTermKey(current);

        if (visited.has(key)) {
            continue;
        }

        visited.add(key);

        for (const q of store.getQuads(current, null, null, g)) {
            quads.push(q);

            if (q.object?.termType === "BlankNode") {
                stack.push(q.object);
            }
        }
    }

    return quads;
}

function collectSubjectClosureFromStore(
    store,
    graphIri,
    subjectIri
) {
    const g = graphTerm(graphIri);
    const root = subjectTerm(subjectIri);
    const direct = store.getQuads(root, null, null, g);

    const blankNodeSeeds = direct
        .map(q => q.object)
        .filter(term => term?.termType === "BlankNode");

    return [
        ...direct,
        ...collectBlankNodeClosureFromStore(
            store,
            graphIri,
            blankNodeSeeds
        )
    ];
}

function collectSubjectClosureFromQuads(quads, subjectIri) {
    const root = subjectTerm(subjectIri);
    const bySubject = new Map();

    for (const q of quads) {
        const key = rdfTermKey(q.subject);

        if (!bySubject.has(key)) {
            bySubject.set(key, []);
        }

        bySubject.get(key).push(q);
    }

    const visited = new Set();
    const stack = [root];
    const out = [];

    while (stack.length) {
        const current = stack.pop();

        if (!current) {
            continue;
        }

        const key = rdfTermKey(current);

        if (visited.has(key)) {
            continue;
        }

        visited.add(key);

        for (const q of bySubject.get(key) || []) {
            out.push(q);

            if (q.object?.termType === "BlankNode") {
                stack.push(q.object);
            }
        }
    }

    return out;
}

function removeSubjectWithBlankNodeClosure(
    store,
    graphIri,
    subjectIri
) {
    const quads = collectSubjectClosureFromStore(
        store,
        graphIri,
        subjectIri
    );

    if (quads.length) {
        store.removeQuads(quads);
    }
}

async function subjectFromStoreToTurtle(
    store,
    graphIri,
    subjectIri
) {
    const quads = collectSubjectClosureFromStore(
        store,
        graphIri,
        subjectIri
    );

    const writer = new window.N3.Writer({
        format: "text/turtle",
        prefixes: PREVIEW_PREFIXES
    });

    for (const q of quads) {
        writer.addQuad(
            window.N3.DataFactory.quad(
                q.subject,
                q.predicate,
                q.object
            )
        );
    }

    return await new Promise((resolve, reject) => {
        writer.end((err, out) => {
            if (err) {
                reject(err);
            } else {
                resolve(out);
            }
        });
    });
}

async function loadMappingTTL(mappingIri) {
    return savedTTL.mappings.get(mappingIri) || await subjectFromStoreToTurtle(dataset, G_MAPPINGS, mappingIri);
}

async function loadEntityTTL(entityIri) {
    return savedTTL.entities.get(entityIri) || await subjectFromStoreToTurtle(dataset, G_ENTITIES, entityIri);
}

function trySerialize(formEl, mime) {
    try {
        const out = formEl.serialize(mime);
        if (typeof out === "string" && out.trim().length > 0) return out;
    } catch { }
    return null;
}

async function toTriGFromSerialized(serialized, format, graphIri) {
    const parser = new window.N3.Parser({ format });
    const quads = parser.parse(serialized);
    const g = window.N3.DataFactory.namedNode(graphIri);
    const hasGraph = quads.some(q => q.graph && q.graph.termType !== "DefaultGraph");
    const finalQuads = hasGraph ? quads : quads.map(q => window.N3.DataFactory.quad(q.subject, q.predicate, q.object, g));
    const writer = new window.N3.Writer({ format: "application/trig", prefixes: PREVIEW_PREFIXES });
    writer.addQuads(finalQuads);
    return await new Promise((resolve, reject) => writer.end((err, out) => (err ? reject(err) : resolve(out))));
}

async function formToTriG(formEl) {
    const graphIri = formEl.getAttribute("data-values-graph");
    const subject = formEl.getAttribute("data-values-subject");
    if (!graphIri) throw new Error("data-values-graph missing on form");
    if (!subject) throw new Error("data-values-subject missing on form");

    const trig = trySerialize(formEl, "application/trig");
    if (trig) return trig;
    const nquads = trySerialize(formEl, "application/n-quads");
    if (nquads) return await toTriGFromSerialized(nquads, "application/n-quads", graphIri);
    const turtleish = trySerialize(formEl, "text/turtle") || trySerialize(formEl, "text/turtle; charset=utf-8");
    if (!turtleish) throw new Error("Could not serialize form.");
    const looksLikeTriG = turtleish.includes("{");
    return await toTriGFromSerialized(turtleish, looksLikeTriG ? "application/trig" : "text/turtle", graphIri);
}

function parseSerializedToQuads(serialized, formatHint = null) {
    const baseIRI = document.baseURI || window.location.href;
    let format = formatHint;
    if (!format) {
        const looksLikeTriG = serialized.includes("{");
        const looksTurtle = serialized.includes("@prefix") || serialized.includes("PREFIX") || serialized.includes(";");
        format = looksLikeTriG ? "application/trig" : (looksTurtle ? "text/turtle" : "application/n-quads");
    }
    const parser = new window.N3.Parser({ format, baseIRI });
    return parser.parse(serialized);
}

async function writeTriG(quads, prefixes = PREVIEW_PREFIXES) {
    const writer = new window.N3.Writer({ format: "application/trig", prefixes });
    writer.addQuads(quads);
    return await new Promise((resolve, reject) => writer.end((e, out) => e ? reject(e) : resolve(out)));
}

async function replaceSubjectInStore(
    store,
    graphIri,
    subjectIri,
    newQuads
) {
    const DF = window.N3.DataFactory;
    const g = DF.namedNode(graphIri);
    const s = DF.namedNode(subjectIri);

    const oldManagedQuads = [];

    for (const p of FORM_PROPS) {
        oldManagedQuads.push(
            ...store.getQuads(
                s,
                DF.namedNode(p),
                null,
                g
            )
        );
    }

    const oldBlankNodeSeeds = oldManagedQuads
        .map(q => q.object)
        .filter(term => term?.termType === "BlankNode");

    if (oldManagedQuads.length) {
        store.removeQuads(oldManagedQuads);
    }

    const oldNestedQuads =
        collectBlankNodeClosureFromStore(
            store,
            graphIri,
            oldBlankNodeSeeds
        );

    if (oldNestedQuads.length) {
        store.removeQuads(oldNestedQuads);
    }

    const subjectClosure =
        collectSubjectClosureFromQuads(
            newQuads,
            subjectIri
        );

    const forced = subjectClosure.map(q =>
        DF.quad(
            q.subject,
            q.predicate,
            q.object,
            g
        )
    );

    if (forced.length) {
        store.addQuads(forced);
    }
}

async function saveFormToLocalOverlay(formEl, which) {
    const graphIri = formEl.getAttribute("data-values-graph");
    const subjectIri = formEl.getAttribute("data-values-subject");
    if (!graphIri || !subjectIri) return;

    const trig = await formToTriG(formEl);
    if (!trig.trim()) return;

    const quads = parseSerializedToQuads(trig, "application/trig");
    const perStore = which === "mappings" ? storeMappings : storeEntities;

    await replaceSubjectInStore(perStore, graphIri, subjectIri, quads);
    await replaceSubjectInStore(dataset, graphIri, subjectIri, quads);

    const turtle = await subjectFromStoreToTurtle(perStore, graphIri, subjectIri);
    savedTTL[which].set(subjectIri, turtle);
    updateSourceInfo();
}

async function previewSelectedNow() {
    if (!selectedMapping && !selectedTarget) return;
    const parts = [];
    if (selectedMapping) parts.push(...parseSerializedToQuads(await formToTriG(mappingForm), "application/trig"));
    if (selectedTarget) parts.push(...parseSerializedToQuads(await formToTriG(entityForm), "application/trig"));
    trigOut.value = await writeTriG(parts, PREVIEW_PREFIXES);
    refreshActions();
}

function debouncePreviewSelected() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => { try { await previewSelectedNow(); } catch { } }, 500);
}

function debounceSyncTarget() {
    clearTimeout(syncTimer);
    syncTimer = setTimeout(async () => {
        if (!selectedMapping) return;
        try {
            const newTarget = await fetchTargetIri(selectedMapping);
            if (!newTarget || newTarget === selectedTarget) return;
            selectedTarget = newTarget;
            const entityTTL = savedTTL.entities.get(selectedTarget) || await loadEntityTTL(selectedTarget);
            entityForm.setAttribute("data-values", entityTTL);
            entityForm.setAttribute("data-values-subject", selectedTarget);
            updateIriHeadersAndCopyState();
            refreshActions();
            debouncePreviewSelected();
        } catch (e) {
            setStatus(String(e));
        }
    }, 250);
}

async function selectMapping(mappingIri) {
    selectedMapping = mappingIri;
    updateIriHeadersAndCopyState();

    [...listEl.querySelectorAll(".item")].forEach(el => {
        const iriText = el.querySelector(".muted")?.textContent || "";
        el.classList.toggle("active", iriText.includes(mappingIri));
    });

    setStatus("Loading mapping…");
    const mappingTTL = await loadMappingTTL(mappingIri);
    mappingForm.setAttribute("data-values", mappingTTL);
    mappingForm.setAttribute("data-values-subject", mappingIri);

    setStatus("Resolving target entity…");
    selectedTarget = await fetchTargetIri(mappingIri);
    updateIriHeadersAndCopyState();

    if (!selectedTarget) {
        entityForm.setAttribute("data-values", "");
        entityForm.removeAttribute("data-values-subject");
        trigOut.value = "";
        refreshActions();
        setStatus("No target entity found for this mapping.");
        return;
    }

    setStatus("Loading entity…");
    const entityTTL = await loadEntityTTL(selectedTarget);
    entityForm.setAttribute("data-values", entityTTL);
    entityForm.setAttribute("data-values-subject", selectedTarget);

    setStatus("");
    refreshActions();
    debouncePreviewSelected();
}

function downloadText(filename, text, mime = "application/trig;charset=utf-8") {
    const blob = new Blob([text], { type: mime });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
}

async function exportFullLocalStore() {
    const writer = new window.N3.Writer({ format: "application/trig", prefixes: PREVIEW_PREFIXES });
    for (const q of dataset.getQuads(null, null, null, null)) writer.addQuad(q);
    return await new Promise((resolve, reject) => writer.end((e, out) => e ? reject(e) : resolve(out)));
}

newMappingBtn.addEventListener("click", async () => {
    const iri = mintIRI("https://data.scigma.de/resources/mappings");
    const now = new Date().toISOString();
    const ttl = `
<${iri}> a <${MAP_ENTRY}> ;
<${MAP_LABEL_P}> "New mapping" ;
<${PROV_GENERATED_AT}> "${now}"^^<http://www.w3.org/2001/XMLSchema#dateTime> ;
<${DCTERMS_CONFORMS_TO}> <https://data.scigma.de/resources/shapes/MappingShape> .
`.trim() + "\n";
    mappingForm.setAttribute("data-values", ttl);
    mappingForm.setAttribute("data-values-subject", iri);
    selectedMapping = iri;
    savedTTL.mappings.set(iri, ttl);
    updateIriHeadersAndCopyState();
    refreshActions();
    debouncePreviewSelected();
    await saveSnapshotToIndexedDb();
    setStatus("New mapping created.");
});

newEntityBtn.addEventListener("click", async () => {
    const iri = mintIRI("https://data.scigma.de/resources/entities");
    const ttl_old = `
<${iri}> a <http://www.w3.org/2002/07/owl#Thing> ;
<${DCTERMS_CONFORMS_TO}> <https://data.scigma.de/resources/shapes/EntityShape> .
`.trim() + "\n";
    const ttl = `
<${iri}> a <http://www.w3.org/2002/07/owl#Thing> ;
    <${PROV_GENERATED_AT}> "${new Date().toISOString()}"^^<http://www.w3.org/2001/XMLSchema#dateTime> .
`.trim() + "\n";

    entityForm.setAttribute("data-values", ttl);
    entityForm.setAttribute("data-values-subject", iri);
    selectedTarget = iri;
    savedTTL.entities.set(iri, ttl);
    updateIriHeadersAndCopyState();
    refreshActions();
    debouncePreviewSelected();
    await saveSnapshotToIndexedDb();
    setStatus("New entity created.");
});

deleteMappingBtn.addEventListener("click", async () => {
    if (!selectedMapping) return;
    removeSubjectWithBlankNodeClosure(
        storeMappings,
        G_MAPPINGS,
        selectedMapping
    );

    removeSubjectWithBlankNodeClosure(
        dataset,
        G_MAPPINGS,
        selectedMapping
    );
    savedTTL.mappings.delete(selectedMapping);
    selectedMapping = null;
    mappingForm.setAttribute("data-values", "");
    mappingForm.removeAttribute("data-values-subject");
    updateIriHeadersAndCopyState();
    refreshActions();
    updateSourceInfo();
    await saveSnapshotToIndexedDb();
    await loadTypeHints();
    await loadMappingList();
    setStatus("Mapping deleted from local store.");
});

deleteEntityBtn.addEventListener("click", async () => {
    if (!selectedTarget) return;
    removeSubjectWithBlankNodeClosure(
        storeEntities,
        G_ENTITIES,
        selectedTarget
    );

    removeSubjectWithBlankNodeClosure(
        dataset,
        G_ENTITIES,
        selectedTarget
    );
    savedTTL.entities.delete(selectedTarget);
    selectedTarget = null;
    entityForm.setAttribute("data-values", "");
    entityForm.removeAttribute("data-values-subject");
    updateIriHeadersAndCopyState();
    refreshActions();
    updateSourceInfo();
    await saveSnapshotToIndexedDb();
    setStatus("Entity deleted from local store.");
});

saveBtn.addEventListener("click", async () => {
    const oldText = saveBtn.textContent;
    try {
        setStatus("Saving locally…");
        if (selectedMapping) await saveFormToLocalOverlay(mappingForm, "mappings");
        if (selectedTarget) await saveFormToLocalOverlay(entityForm, "entities");
        saveBtn.textContent = "Saved ✓";
        setTimeout(() => (saveBtn.textContent = oldText), 900);
        rebuildPerGraphStores();
        await saveSnapshotToIndexedDb();
        await loadTypeHints();
        await loadMappingList();
        await previewSelectedNow();
        setStatus("");
    } catch (e) {
        saveBtn.textContent = oldText;
        setStatus(String(e));
    }
});

showMappingBtn.addEventListener("click", async () => {
    try { trigOut.value = await formToTriG(mappingForm); refreshActions(); }
    catch (e) { setStatus(String(e)); }
});

showEntityBtn.addEventListener("click", async () => {
    try { trigOut.value = await formToTriG(entityForm); refreshActions(); }
    catch (e) { setStatus(String(e)); }
});

showSelectedBtn.addEventListener("click", async () => {
    try {
        if (!selectedMapping && !selectedTarget) { setStatus("Nothing selected."); return; }
        await previewSelectedNow();
        refreshActions();
    } catch (e) { setStatus(String(e)); }
});

copyTrigBtn.addEventListener("click", async () => {
    const ok = await copyText(trigOut.value || "");
    setStatus(ok ? "Copied." : "Copy failed.");
    setTimeout(() => setStatus(""), 800);
});

clearTrigBtn.addEventListener("click", () => {
    trigOut.value = "";
    refreshActions();
});

exportMappingBtn.addEventListener("click", async () => {
    try { downloadText("mapping.trig", await formToTriG(mappingForm)); }
    catch (e) { setStatus(String(e)); }
});

exportEntityBtn.addEventListener("click", async () => {
    try { downloadText("entity.trig", await formToTriG(entityForm)); }
    catch (e) { setStatus(String(e)); }
});

exportSelectedBtn.addEventListener("click", async () => {
    try {
        const writer = new window.N3.Writer({ format: "application/trig", prefixes: PREVIEW_PREFIXES });
        if (selectedMapping) {
            for (const q of parseSerializedToQuads(await formToTriG(mappingForm), "application/trig")) writer.addQuad(q);
        }
        if (selectedTarget) {
            for (const q of parseSerializedToQuads(await formToTriG(entityForm), "application/trig")) writer.addQuad(q);
        }
        const out = await new Promise((resolve, reject) => writer.end((e, s) => e ? reject(e) : resolve(s)));
        downloadText("mapping+entity.trig", out);
    } catch (e) {
        setStatus(String(e));
    }
});

exportFullBtn.addEventListener("click", async () => {
    try {
        setStatus("Exporting full local store…");
        const trig = await exportFullLocalStore();
        downloadText("full-graphs.trig", trig);
        setStatus("Download finished.");
        setTimeout(() => setStatus(""), 1200);
    } catch (e) {
        setStatus(String(e));
    }
});

prevBtn.addEventListener("click", async () => {
    page = Math.max(0, page - 1);
    pageEl.textContent = String(page + 1);
    try { await loadMappingList(); } catch (e) { setStatus(String(e)); }
});

nextBtn.addEventListener("click", async () => {
    page += 1;
    pageEl.textContent = String(page + 1);
    try { await loadMappingList(); } catch (e) { setStatus(String(e)); }
});

reloadBtn.addEventListener("click", async () => {
    try { await loadTypeHints(); await loadMappingList(); } catch (e) { setStatus(String(e)); }
});

let searchTimer = null;
function triggerSearchReload() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
        page = 0;
        pageEl.textContent = "1";
        try { await loadMappingList(); } catch (e) { setStatus(String(e)); }
    }, 250);
}
qEl.addEventListener("input", triggerSearchReload);
typeHintFilterEl.addEventListener("change", triggerSearchReload);

openFileBtn.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", async () => {
    const f = fileInput.files?.[0];
    if (!f) return;
    try {
        const text = await f.text();
        fileInfo.textContent = `Loaded local: ${f.name} (${formatBytes(f.size)})`;
        await loadFileTextIntoStores(text, f.name);
        page = 0;
        pageEl.textContent = "1";
        await loadTypeHints();
        await loadMappingList();
    } catch (e) {
        setStatus(String(e));
    } finally {
        fileInput.value = "";
    }
});

clearLocalBtn.addEventListener("click", async () => {
    clearLocalStores();
    await clearSnapshotFromIndexedDb();
    clearSelection();
    fileInfo.textContent = "No local file loaded.";
    page = 0;
    pageEl.textContent = "1";
    await loadTypeHints();
    await loadMappingList();
});

loadRemoteBtn.addEventListener("click", async () => {
    try {
        await importRemoteGraphsIntoLocal();
        page = 0;
        pageEl.textContent = "1";
        await loadTypeHints();
        await loadMappingList();
    } catch (e) {
        setStatus(String(e));
    }
});

clearEndpointBtn.addEventListener("click", () => {
    remoteEndpoint = "";
    endpointEl.value = "";
    updateSourceInfo();
});

mappingForm.addEventListener("change", () => { debounceSyncTarget(); debouncePreviewSelected(); });
entityForm.addEventListener("change", debouncePreviewSelected);

initForms();
wireCopyOnce(copyMappingIriEl, () => selectedMapping || "");
wireCopyOnce(copyEntityIriEl, () => selectedTarget || "");
updateIriHeadersAndCopyState();
refreshActions();
listEl.innerHTML = `<div class="muted"><span class="spinner"></span>Restoring local snapshot…</div>`;

try {
    await loadSnapshotFromIndexedDb();
} catch (e) {
    console.warn("Could not restore snapshot:", e);
}

updateSourceInfo();

if (!dataset.size) {
    listEl.innerHTML = `<div class="muted">Load a local TriG file and/or import a remote endpoint snapshot.</div>`;
}

await loadTypeHints();
await loadMappingList();
