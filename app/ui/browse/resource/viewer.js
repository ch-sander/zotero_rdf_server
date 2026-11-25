// const ENDPOINT = "http://localhost:7879/query";
const DEFAULT_ENDPOINT = "/sparql/query";

const DEFAULT_ONTOLOGY_GRAPH = "http://www.zotero.org/namespaces/export";
const ZOTERO_NS = "http://www.zotero.org/namespaces/export#";
// const ONTOLOGY_GRAPH = null;

const params = new URLSearchParams(location.search);
const QUERY_UI = "/sparql/"
const ENDPOINT =
  params.get("endpoint")
  || DEFAULT_ENDPOINT;

const ONTOLOGY_GRAPH =
  params.get("ontology")
  || DEFAULT_ONTOLOGY_GRAPH;

console.log("SPARQL endpoint:", ENDPOINT);

const PROPERTY_FILTERS = params.getAll("property");
const VIEW_MODE = params.get("view");
// Fast mode keeps direct labels but skips type badges and transitive expansion.
const FAST_MODE = params.get("mode") !== "full";

const HIDDEN_PROPERTIES = new Set([
  "http://www.zotero.org/namespaces/export#version",
  "http://www.w3.org/2000/01/rdf-schema#label",
  "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
  "http://www.w3.org/2000/01/rdf-schema#comment",
  "http://www.w3.org/2002/07/owl#sameAs",
  "http://purl.org/dc/elements/1.1/relation",
  "http://www.w3.org/ns/prov#generatedAtTime",
  "http://www.zotero.org/namespaces/export#links",
  "http://www.zotero.org/namespaces/export#href",
  "http://www.zotero.org/namespaces/export#relations",
  "http://www.zotero.org/namespaces/export#url"
]);

const allowedHosts = new Set([
  "app.scigma.de",
  "localhost",
  "127.0.0.1"
]);

const allowedTypes = new Set([
  "http://www.zotero.org/namespaces/export#Agent",
  "http://www.zotero.org/namespaces/export#Place",
  "http://www.zotero.org/namespaces/export#Tag",
  "http://www.zotero.org/namespaces/export#Publisher",
  "https://zotero-rdf-server.org/mapping/Entry"
]);

const LANGUAGE = "en";
const LIMIT = 1000;
const LABEL_BATCH_SIZE = 256;
const preferredLabelCache = new Map();
const allLanguageLabelCache = new Map();
const LOG_TIMINGS = params.get("timings") === "1";
const FAST_RELATION_PREFIXES = `
  PREFIX owl:  <http://www.w3.org/2002/07/owl#>
  PREFIX dc:   <http://purl.org/dc/elements/1.1/>
  PREFIX dct:  <http://purl.org/dc/terms/>
`;

const statusEl = document.querySelector("#status");
const contentEl = document.querySelector("#content");
const triplesEl = document.querySelector("#triples");
const resourceUriEl = document.querySelector("#resource-uri");
const resourceLabelEl = document.querySelector("#resource-label");
const generatedAtEl = document.querySelector("#generated-at");
const commentBoxEl = document.querySelector("#comment-box");
const commentTextEl = document.querySelector("#comment-text");
const resourceTypesEl = document.querySelector("#resource-types");
const sameAsBoxEl = document.querySelector("#sameas-box");
const sameAsListEl = document.querySelector("#sameas-list");
const relatedBoxEl = document.querySelector("#related-box");
const relatedListEl = document.querySelector("#related-list");
const incomingSectionEl = document.querySelector("#incoming-section");
const incomingTriplesEl = document.querySelector("#incoming-triples");

let activeLoad;

window.addEventListener("hashchange", () => {
  loadCurrentResource();
});

installFastModeToggle();
loadCurrentResource();

function installFastModeToggle() {
  document.documentElement.dataset.queryMode = FAST_MODE ? "fast" : "full";

  const button = document.createElement("button");
  button.type = "button";
  button.textContent = FAST_MODE ? "Fast mode: on" : "Fast mode: off";
  button.title = FAST_MODE
    ? "Direct relations only; click to enable full transitive queries"
    : "Click to use cheaper queries without transitive property paths";
  button.setAttribute("aria-pressed", String(FAST_MODE));

  button.addEventListener("click", () => {
    const url = new URL(window.location.href);

    if (FAST_MODE) {
      url.searchParams.set("mode", "full");
    } else {
      url.searchParams.set("mode", "fast");
    }

    window.location.assign(url.toString());
  });

  button.className = "wk-button wk-mode";
  document.getElementById("view-options").append(button);
}

function setEditLink(resourceIri, resourceTypes = []) {
  const editLink = document.getElementById("edit-resource-link");
  if (!editLink) return;

  if (!resourceIri || !allowedHosts.has(window.location.hostname) ||
      !resourceTypes.some(type => allowedTypes.has(type))) {
    editLink.hidden = true;
    editLink.removeAttribute("href");
    return;
  }

  const editUrl =
    new URL("../../edit/", document.baseURI);

  const parameterName =
    resourceIri.includes("/mappings/")
      ? "mapping"
      : "entity";

  editUrl.searchParams.set(parameterName, resourceIri);
  editUrl.searchParams.set("endpoint", LibraryWorkspace.linkEndpoint(ENDPOINT));

  editLink.href = editUrl.toString();
  editLink.hidden = false;
}

function clearOutgoing() {
  triplesEl.innerHTML = "";
  resourceLabelEl.textContent = "";
  resourceTypesEl.innerHTML = "";
  commentTextEl.innerHTML = "";
  commentBoxEl.hidden = true;
  generatedAtEl.hidden = true;
  generatedAtEl.textContent = "";
}

async function loadCurrentResource() {
  activeLoad?.abort();
  activeLoad = new AbortController();
  const { signal } = activeLoad;
  const uri = getUriFromLocation();
  const isCurrent = () => !signal.aborted && getUriFromLocation() === uri;

  clearZoteroMetadata();
  clearOutgoing();
  setEditLink(uri, []);
  contentEl.hidden = true;

  sameAsListEl.innerHTML = "";
  sameAsBoxEl.hidden = true;
  relatedListEl.innerHTML = "";
  relatedBoxEl.hidden = true;

  incomingTriplesEl.innerHTML = "";
  incomingSectionEl.hidden = true;
  if (!uri) {
    showStatus("Open a resource from the class browser, or append its IRI after # in the URL.");
    return;
  }

  resourceUriEl.innerHTML = "";

  const span = document.createElement("span");
  span.className = "resource-uri-text";
  span.textContent = uri;

  resourceUriEl.appendChild(withCopy(span, uri));
  showStatus("Loading Resource …");

  try {
    const resolvedUri = await resolveUri(uri, signal);
    if (!isCurrent()) return;

    resourceUriEl.appendChild(createQueryLink(resolvedUri));
    resourceUriEl.appendChild(createRdfDownloadLink(resolvedUri));

    if (FAST_MODE) {
      await loadFastView(resolvedUri, signal);
      return;
    }

    const data = await queryResource(resolvedUri, signal);
    if (!isCurrent()) return;
    const bindings = data.results.bindings;
    if (bindings.length === 0) {
      showStatus("No triples!");
      return;
    }

    showResource(bindings);
    const incomingData = await queryIncoming(resolvedUri, signal);
    const sameAsData = await querySameAs(resolvedUri, uri, signal);
    const relatedData = await queryRelated(resolvedUri, signal);
    if (!isCurrent()) return;
    renderIncomingTriples(incomingData.results.bindings);
    renderSameAs(sameAsData.results.bindings);
    renderRelated(relatedData.results.bindings);
    await publishZoteroMetadata(bindings, resolvedUri, uri, signal);
  } catch (error) {
    if (!isCurrent()) return;
    console.error(error);
    showStatus(contentEl.hidden ? "Error" : "Could not finish loading the resource.");
  }
}

function showResource(bindings) {
  renderTriples(bindings);
  hideStatus();
  contentEl.hidden = false;
}

function clearZoteroMetadata() {
  const tags = document.head.querySelectorAll("meta[data-zotero-resource]");
  tags.forEach(tag => tag.remove());
  document.title = "RDF Resource Viewer";
  if (tags.length) document.dispatchEvent(new Event("ZoteroItemUpdated", { bubbles: true }));
}

async function publishZoteroMetadata(bindings, resolvedUri, requestedUri, signal) {
  const values = new Map();
  for (const { p, o } of bindings) {
    if (!values.has(p.value)) values.set(p.value, []);
    values.get(p.value).push(o.value);
  }
  const first = property => values.get(ZOTERO_NS + property)?.[0];
  const itemType = first("itemType");
  const title = first("title") || values.get("http://www.w3.org/2000/01/rdf-schema#label")?.[0];
  if (!itemType || !title) return; // Don't advertise RDF entities as bibliographic items.

  let linkedMetadata = [];
  try {
    const query = `
      PREFIX zot: <${ZOTERO_NS}>
      PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
      PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
      SELECT DISTINCT ?firstName ?lastName ?name ?label ?creatorType ?roleType ?roleLabel
                      ?field ?entity ?entityLabel WHERE {
        {
          <${escapeSparqlIri(resolvedUri)}> zot:creators ?role .
          ?role zot:hasCreator ?person .
          OPTIONAL { ?role zot:creatorType ?creatorType . }
          OPTIONAL { ?role rdf:type ?roleType . FILTER(?roleType IN (zot:author, zot:editor)) }
          OPTIONAL { ?role rdfs:label ?roleLabel . }
          OPTIONAL { ?person zot:firstName ?firstName . }
          OPTIONAL { ?person zot:lastName ?lastName . }
          OPTIONAL { ?person zot:name ?name . }
          OPTIONAL { ?person rdfs:label ?label . }
        } UNION {
          VALUES ?field { zot:publisher zot:place }
          <${escapeSparqlIri(resolvedUri)}> ?field ?entity .
          ?entity rdfs:label ?entityLabel .
        }
      }
      LIMIT 100
    `;
    linkedMetadata = (await selectQuery(query, signal, "Zotero metadata")).results.bindings;
  } catch (error) {
    if (signal?.aborted) return;
    console.warn("Zotero linked metadata unavailable:", error);
  }
  if (signal?.aborted || getUriFromLocation() !== requestedUri) return;

  function addMeta(attribute, name, content) {
    if (!content) return;
    const tag = document.createElement("meta");
    tag.setAttribute(attribute, name);
    tag.content = content;
    tag.dataset.zoteroResource = "";
    document.head.appendChild(tag);
  }

  function addCreator(type, name) {
    addMeta("name", `citation_${type}`, name);
    if (type === "author") addMeta("property", "dc.creator", name);
  }

  function entityValue(property) {
    const field = ZOTERO_NS + property;
    const labels = bindings.filter(binding => binding.p.value === field).map(binding => {
      if (binding.o.type !== "uri") return binding.o.value;
      return binding.oLabel?.value || linkedMetadata.find(row =>
        row.field?.value === field && row.entity?.value === binding.o.value
      )?.entityLabel?.value;
    }).filter(Boolean);
    return [...new Set(labels)].join("; ");
  }

  addMeta("property", "z:itemType", itemType);
  addMeta("name", "citation_title", title);
  addMeta("name", "citation_date", first("date"));
  addMeta("name", "citation_language", first("language"));
  addMeta("name", "citation_volume", first("volume"));
  addMeta("name", "citation_issue", first("issue"));
  addMeta("name", "citation_doi", first("DOI"));
  addMeta("name", "citation_isbn", first("ISBN"));
  addMeta("name", "citation_abstract", first("abstractNote"));
  addMeta("name", "citation_publisher", entityValue("publisher"));
  addMeta("property", "z:place", entityValue("place"));
  if (itemType === "journalArticle") {
    addMeta("name", "citation_journal_title", first("publicationTitle"));
  } else if (itemType === "bookSection") {
    addMeta("name", "citation_inbook_title", first("publicationTitle"));
  }
  addMeta("name", "citation_public_url", requestedUri);
  addMeta("property", "z:url", requestedUri);

  const seen = new Set();
  const foundTypes = new Set();
  for (const creator of linkedMetadata) {
    const roleLabel = creator.roleLabel?.value;
    const type = [
      creator.creatorType?.value?.split(/[/#]/).pop(),
      creator.roleType?.value?.split(/[/#]/).pop(),
      roleLabel?.match(/\bas\s+(author|editor)$/i)?.[1]
    ].map(value => value?.toLowerCase()).find(value => value === "author" || value === "editor");
    if (!type) continue;
    const lastName = creator.lastName?.value;
    const firstName = creator.firstName?.value;
    const name = lastName
      ? [lastName, firstName].filter(Boolean).join(", ")
      : creator.name?.value || creator.label?.value || firstName
        || roleLabel?.replace(/\s+as\s+(author|editor)$/i, "");
    const key = `${type}:${name}`;
    if (!name || seen.has(key)) continue;
    seen.add(key);
    foundTypes.add(type);
    addCreator(type, name);
  }
  const missingTypes = new Set(["author", "editor"].filter(type => !foundTypes.has(type)));
  for (const binding of bindings.filter(row => row.p.value === ZOTERO_NS + "creators")) {
    const match = binding.oLabel?.value?.match(/^(.+?)\s+as\s+(author|editor)$/i);
    if (!match) continue;
    const name = match[1].trim();
    const type = match[2].toLowerCase();
    if (missingTypes.has(type) && name) addCreator(type, name);
  }
  document.title = title;
  document.dispatchEvent(new Event("ZoteroItemUpdated", { bubbles: true }));
}

function getUriFromLocation() {
  if (location.hash.length > 1) {
    return decodeURIComponent(location.hash.slice(1));
  }

  return new URLSearchParams(location.search).get("uri");
}

async function selectQuery(query, signal, description = "SPARQL") {
  LibraryWorkspace.record(description, "POST " + ENDPOINT, query);
  const start = LOG_TIMINGS ? performance.now() : 0;
  const response = await fetch(ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/sparql-query",
      "Accept": "application/sparql-results+json"
    },
    body: query,
    signal
  });

  if (!response.ok) {
    const body = await response.text();
    let detail = body;
    try {
      const error = JSON.parse(body);
      detail = error.exception || error.message || error.error || body;
    } catch { /* Some proxies send a plain-text error. */ }
    throw new Error(`HTTP ${response.status} ${String(detail).slice(0, 280)}`);
  }

  const data = await response.json();
  if (!Array.isArray(data?.results?.bindings)) {
    throw new Error(data?.exception || data?.message || "Expected SPARQL JSON result bindings.");
  }
  if (LOG_TIMINGS) {
    console.info(`[viewer] ${description} — ${Math.round(performance.now() - start)} ms, ${data.results.bindings.length} rows`);
  }
  return data;
}

function queryResource(uri, signal) {
  return selectQuery(buildQuery(uri), signal, "Outgoing triples");
}

function buildFastIncoming(uri, visibleOnly = false) {
  const visibilityFilter = visibleOnly
    ? [...HIDDEN_PROPERTIES].map(iri => `FILTER(?p != <${iri}>)`).join("\n")
    : "";
  return `
    SELECT ?s ?p WHERE {
      ?s ?p <${escapeSparqlIri(uri)}> .
      FILTER(isIRI(?s))
      ${visibilityFilter}
    }
    ORDER BY ?p ?s
    LIMIT ${LIMIT}
  `;
}

function buildFastSameAs(uri, requestedUri = uri) {
  const target = `<${escapeSparqlIri(uri)}>`;
  return `
    SELECT DISTINCT ?same WHERE {
      { ${target} owl:sameAs ?same } UNION { ?same owl:sameAs ${target} }
      FILTER(isIRI(?same) && ?same != <${escapeSparqlIri(requestedUri)}>)
    }
    ORDER BY ?same
    LIMIT ${LIMIT}
  `;
}

function buildFastRelated(uri) {
  const target = `<${escapeSparqlIri(uri)}>`;
  return `
    SELECT DISTINCT ?related WHERE {
      { ${target} dc:relation ?related } UNION { ?related dc:relation ${target} }
      UNION { ${target} dct:relation ?related } UNION { ?related dct:relation ${target} }
      FILTER(isIRI(?related))
    }
    ORDER BY ?related
    LIMIT ${LIMIT}
  `;
}

function buildLabelQuery(iris, anyLanguage) {
  const terms = iris.map(iri => `(<${escapeSparqlIri(iri)}>)`).join(" ");
  return `
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    SELECT ?node (MIN(?text) AS ?label) WHERE {
      VALUES (?node) { ${terms} }
      ?node rdfs:label ?text .
      ${anyLanguage ? "" : `FILTER(lang(?text) = "${LANGUAGE}" || lang(?text) = "")`}
    }
    GROUP BY ?node
  `;
}

function createLabelLookup(signal, anyLanguage, report) {
  const cache = anyLanguage ? allLanguageLabelCache : preferredLabelCache;
  const pending = new Map();
  const queue = [];
  let running = false;

  async function drain() {
    while (queue.length) {
      const iris = queue.splice(0, LABEL_BATCH_SIZE);
      const found = new Map();
      let succeeded = false;
      try {
        if (!signal.aborted) {
          const { results } = await selectQuery(buildLabelQuery(iris, anyLanguage), signal,
            anyLanguage ? "Related labels" : "Resource labels");
          for (const { node, label } of results.bindings) {
            if (node && label) found.set(node.value, label);
          }
          succeeded = !signal.aborted;
        }
      } catch (error) {
        report("labels", error);
      }
      const expires = Date.now() + 60_000;
      for (const iri of iris) {
        const label = succeeded ? found.get(iri) || null : null;
        if (succeeded) {
          cache.set(iri, { label, expires });
          if (cache.size > 4096) cache.delete(cache.keys().next().value);
        }
        pending.get(iri).resolve(label);
        pending.delete(iri);
      }
    }
    running = false;
  }

  return function lookup(iri) {
    if (signal.aborted) return Promise.resolve(null);
    const cached = cache.get(iri);
    if (cached && cached.expires > Date.now()) return Promise.resolve(cached.label);
    if (!pending.has(iri)) {
      let resolve;
      const promise = new Promise(done => { resolve = done; });
      pending.set(iri, { promise, resolve });
      queue.push(iri);
      if (!running) {
        running = true;
        Promise.resolve().then(drain);
      }
    }
    return pending.get(iri).promise;
  };
}

function buildBlankLabelQuery(uri) {
  // Blank node identifiers are scoped to one result. Return each object together
  // with its label in a fresh, complete snapshot of the outgoing rows.
  return `
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    SELECT ?p ?o ?oLabel WHERE {
      { ${buildQuery(uri)} }
      OPTIONAL {
        SELECT ?p ?o (MIN(?text) AS ?oLabel) WHERE {
          <${escapeSparqlIri(uri)}> ?p ?o .
          FILTER(isBlank(?o))
          ?o rdfs:label ?text .
          FILTER(lang(?text) = "${LANGUAGE}" || lang(?text) = "")
        }
        GROUP BY ?p ?o
      }
    }
    ORDER BY ?p ?o
  `;
}

function linkStatus(box, entries) {
  const inTable = entries === incomingTriplesEl;
  const item = document.createElement(inTable ? "tr" : "li");
  let content = item;
  if (inTable) {
    content = document.createElement("td");
    content.colSpan = 2;
    item.appendChild(content);
  }
  item.className = "viewer-link-status";
  const note = document.createElement("span");
  note.className = "value-wrap";
  note.setAttribute("role", "status");
  content.appendChild(note);
  entries.appendChild(item);
  box.hidden = false;
  return (message, busy) => {
    box.setAttribute("aria-busy", String(busy));
    if (message) note.textContent = message;
    else item.remove();
  };
}

async function loadFastView(uri, signal) {
  const isCurrent = () => !signal.aborted && getUriFromLocation() === uri;
  const failures = new Set();
  let hasRows = false;

  function report(section, error) {
    if (!isCurrent()) return;
    failures.add(section);
    console.warn(`Could not load ${section}`, error);
    showStatus(`Could not load ${[...failures].join(", ")}. ${error.message}`);
  }

  const lookup = createLabelLookup(signal, false, report);
  const lookupRelated = createLabelLookup(signal, true, report);
  async function loadLinks(name, box, entries, request, render, lookupLabel) {
    const status = linkStatus(box, entries);
    status(`Loading ${name} …`, true);
    try {
      const { results } = await request;
      if (!isCurrent()) return;
      if (!results.bindings.length) {
        status(`No ${name}.`, false);
        return;
      }
      hasRows = true;
      const labels = new Set();
      render(results.bindings, iri => {
        const label = lookupLabel(iri);
        labels.add(label);
        return label;
      });
      if (labels.size) {
        status("Loading labels …", true);
        await Promise.all(labels);
      }
      if (isCurrent()) status("", false);
    } catch (error) {
      if (!isCurrent()) return;
      status(`Could not load ${name}.`, false);
      report(name, error);
    }
  }

  async function loadOutgoing() {
    const { results } = await queryResource(uri, signal);
    if (!isCurrent()) return;
    if (!failures.size) hideStatus();
    let rows = results.bindings;
    if (!rows.length) return;
    hasRows = true;
    renderTriples(rows, lookup);
    if (rows.some(row => row.o.type === "bnode")) {
      try {
        const data = await selectQuery(buildBlankLabelQuery(uri), signal, "Blank node labels");
        if (!isCurrent()) return;
        rows = data.results.bindings;
        clearOutgoing();
        renderTriples(rows, lookup);
      } catch (error) {
        report("blank node labels", error);
      }
    }
    if (!isCurrent() || !rows.some(row => row.p.value === ZOTERO_NS + "itemType")) return;
    const fields = new Set(["creators", "publisher", "place"].map(key => ZOTERO_NS + key));
    await Promise.all(rows.filter(row => fields.has(row.p.value) && row.o.type === "uri" && !row.oLabel?.value)
      .map(async row => { row.oLabel = await lookup(row.o.value); }));
    if (isCurrent()) await publishZoteroMetadata(rows, uri, uri, signal);
  }

  contentEl.hidden = false;
  await Promise.all([
    loadOutgoing().catch(error => report("outgoing triples", error)),
    loadLinks("incoming links", incomingSectionEl, incomingTriplesEl,
      queryIncoming(uri, signal, true), renderIncomingTriples, lookup),
    loadLinks("sameAs links", sameAsBoxEl, sameAsListEl,
      querySameAs(uri, uri, signal), renderSameAs, lookup),
    loadLinks("related links", relatedBoxEl, relatedListEl,
      queryRelated(uri, signal), renderRelated, lookupRelated)
  ]);
  if (isCurrent() && !hasRows && !failures.size) showStatus("No triples!");
}

async function queryIncoming(uri, signal, visibleOnly = false) {
  if (FAST_MODE) {
    return selectQuery(buildFastIncoming(uri, visibleOnly), signal, "Incoming links");
  }

  const propertyLabelBlock = ONTOLOGY_GRAPH
    ? `
      OPTIONAL {
        GRAPH <${ONTOLOGY_GRAPH}> {
          ?p rdfs:label ?pl .
          FILTER(lang(?pl) = "${LANGUAGE}" || lang(?pl) = "")
        }
      }
    `
    : `
      OPTIONAL {
        ?p rdfs:label ?pl .
        FILTER(lang(?pl) = "${LANGUAGE}" || lang(?pl) = "")
      }
    `;

  const query = `
      PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
      PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
      SELECT ?s (MIN(?sl) AS ?sLabel)
            ?p (MIN(?pl) AS ?pLabel)
            (GROUP_CONCAT(DISTINCT STR(?type); separator=" · ") AS ?sType)
      WHERE {
        ?s ?p <${escapeSparqlIri(uri)}> .
        OPTIONAL {
          ?s rdf:type ?type .
        }
        OPTIONAL {
          ?s rdfs:label ?sl .
          FILTER(lang(?sl) = "${LANGUAGE}" || lang(?sl) = "")
        }

        ${propertyLabelBlock}
      }

      GROUP BY ?s ?p
      ORDER BY LCASE(STR(COALESCE(MIN(?pl), ?p)))
               LCASE(STR(COALESCE(MIN(?sl), ?s)))

      LIMIT ${LIMIT}
    `;

  return selectQuery(query, signal, "Incoming links");
}

async function querySameAs(uri, requestedUri, signal) {
  if (FAST_MODE) {
    return selectQuery(`${FAST_RELATION_PREFIXES} ${buildFastSameAs(uri, requestedUri)}`, signal, "sameAs links");
  }

  const query = `
      PREFIX owl:  <http://www.w3.org/2002/07/owl#>
      PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
      PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>

      SELECT ?same
            (MIN(?label) AS ?sameLabel)
            (GROUP_CONCAT(DISTINCT STR(?type); separator=" · ") AS ?sameType)
      WHERE {

        <${escapeSparqlIri(uri)}>
          (owl:sameAs|^owl:sameAs)* ?same .

        FILTER(isIRI(?same))
        FILTER(?same != <${escapeSparqlIri(requestedUri)}>)

        OPTIONAL {
          ?same rdf:type ?type .
        }

        OPTIONAL {
          ?same rdfs:label ?label .
          FILTER(lang(?label) = "${LANGUAGE}" || lang(?label) = "")
        }
      }

      GROUP BY ?same
      ORDER BY LCASE(STR(COALESCE(MIN(?label), ?same)))
    `;

  return selectQuery(query, signal, "sameAs links");
}

function buildQuery(uri) {
  if (FAST_MODE) {
    return `
      SELECT ?p ?o WHERE { <${escapeSparqlIri(uri)}> ?p ?o }
      ORDER BY ?p ?o
      LIMIT ${LIMIT}
    `;
  }

  const propertyLabelBlock = ONTOLOGY_GRAPH
    ? `
      OPTIONAL {
        GRAPH <${ONTOLOGY_GRAPH}> {
          ?p rdfs:label ?pl .
          FILTER(lang(?pl) = "${LANGUAGE}" || lang(?pl) = "")
        }
      }
    `
    : `
      OPTIONAL {
        ?p rdfs:label ?pl .
        FILTER(lang(?pl) = "${LANGUAGE}" || lang(?pl) = "")
      }
    `;

  return `
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX owl: <http://www.w3.org/2002/07/owl#>
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    SELECT ?p (MIN(?pl) AS ?pLabel)
          ?o (MIN(?ol) AS ?oLabel)
          (GROUP_CONCAT(DISTINCT STR(?type); separator=" · ") AS ?oType)
          (MAX(?knownInt) AS ?isKnown)
    WHERE {
      <${escapeSparqlIri(uri)}> ?p ?o .

      BIND(
        IF(
          !isIRI(?o) || EXISTS { ?o ?anyP ?anyO },
          1,
          0
        )
        AS ?knownInt
      )

      ${propertyLabelBlock}

      OPTIONAL {
        ?o rdf:type ?type .
      }

      OPTIONAL {
        ?o rdfs:label ?ol .
        FILTER(lang(?ol) = "${LANGUAGE}" || lang(?ol) = "")
      }
    }
    GROUP BY ?p ?o
    ORDER BY LCASE(STR(COALESCE(MIN(?pl), ?p)))
    LIMIT ${LIMIT}
  `;
}

function isRdfsLabel(p) {
  return p === "http://www.w3.org/2000/01/rdf-schema#label";
}

function isRdfType(p) {
  return p === "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";
}

function isGeneratedAtTime(p) {
  return p === "http://www.w3.org/ns/prov#generatedAtTime";
}

function isRdfsComment(p) {
  return p === "http://www.w3.org/2000/01/rdf-schema#comment";
}


function withCopy(node, value) {
  const wrap = document.createElement("span");
  wrap.className = "value-wrap";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "copy-btn";
  button.textContent = "⧉";
  button.title = "Copy to Clipboard (may not work)";

  button.addEventListener("click", async () => {
    await navigator.clipboard.writeText(value);
    button.textContent = "✓";
    setTimeout(() => button.textContent = "⧉", 900);
  });

  wrap.appendChild(node);
  wrap.appendChild(button);

  return wrap;
}

async function resolveUri(uri, signal) {

  if (FAST_MODE) {
    return uri;
  }

  const query = `
    PREFIX owl: <http://www.w3.org/2002/07/owl#>

    SELECT ?canonical WHERE {

      ?canonical owl:sameAs* <${escapeSparqlIri(uri)}> .

      ?canonical ?p ?o .
    }
    ORDER BY IF(?canonical = <${escapeSparqlIri(uri)}>, 0, 1)
    LIMIT 1
  `;

  const data = await selectQuery(query, signal, "Resolve resource IRI");

  return data.results.bindings[0]?.canonical?.value || uri;
}

async function queryRelated(uri, signal) {
  if (FAST_MODE) {
    return selectQuery(`${FAST_RELATION_PREFIXES} ${buildFastRelated(uri)}`, signal, "Related links");
  }

  const query_oxigraph = `
    PREFIX owl:  <http://www.w3.org/2002/07/owl#>
    PREFIX dc:   <http://purl.org/dc/elements/1.1/>
    PREFIX dct:  <http://purl.org/dc/terms/>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>

    SELECT
      (IRI(MIN(STR(?variant))) AS ?related)
      (SAMPLE(?label) AS ?relatedLabel)
      (GROUP_CONCAT(DISTINCT STR(?type); separator=" · ") AS ?relatedType)
      (MAX(?knownInt) AS ?isKnown)
    WHERE {
      <${escapeSparqlIri(uri)}>
        (owl:sameAs|^owl:sameAs)* ?source .

      ?source
        (dc:relation|^dc:relation|dct:relation|^dct:relation)
        ?rawRelated .

      FILTER(isIRI(?rawRelated))

      ?rawRelated
        (owl:sameAs|^owl:sameAs)* ?variant .

      FILTER(isIRI(?variant))

      {
        SELECT ?rawRelated (MIN(STR(?same)) AS ?cluster)
        WHERE {
          ?rawRelated (owl:sameAs|^owl:sameAs)* ?same .
          FILTER(isIRI(?same))
        }
        GROUP BY ?rawRelated
      }

      OPTIONAL {
        ?variant rdfs:label ?label .
      }

      OPTIONAL {
        ?variant rdf:type ?type .
      }

      BIND(IF(EXISTS { ?variant ?anyP ?anyO }, 1, 0) AS ?knownInt)
    }

    GROUP BY ?cluster
    ORDER BY LCASE(STR(COALESCE(SAMPLE(?label), MIN(STR(?variant)))))
    LIMIT ${LIMIT}
  `;
  const fullQuery = `
    PREFIX owl:  <http://www.w3.org/2002/07/owl#>
    PREFIX dc:   <http://purl.org/dc/elements/1.1/>
    PREFIX dct:  <http://purl.org/dc/terms/>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>

    SELECT
      (IRI(MIN(STR(?variant))) AS ?related)
      (SAMPLE(?labels) AS ?relatedLabel)
      (GROUP_CONCAT(
        DISTINCT STR(?type);
        separator=" · "
      ) AS ?relatedType)
      (MAX(?knownInt) AS ?isKnown)
    WHERE {
      <${escapeSparqlIri(uri)}>
        (owl:sameAs|^owl:sameAs)* ?source .

      ?source
        (dc:relation|^dc:relation|dct:relation|^dct:relation)
        ?rawRelated .

      FILTER(isIRI(?rawRelated))

      ?rawRelated
        (owl:sameAs|^owl:sameAs)* ?variant .

      FILTER(isIRI(?variant))

      {
        SELECT ?rawRelated
              (MIN(STR(?same)) AS ?cluster)
        WHERE {
          ?rawRelated
            (owl:sameAs|^owl:sameAs)* ?same .

          FILTER(isIRI(?same))
        }
        GROUP BY ?rawRelated
      }

      OPTIONAL {
        {
          SELECT ?rawRelated
                (GROUP_CONCAT(
                    DISTINCT STR(?labelValue);
                    separator=" · "
                  ) AS ?labels)
          WHERE {
            ?rawRelated
              (owl:sameAs|^owl:sameAs)* ?labelVariant .

            ?labelVariant rdfs:label ?labelValue .
          }
          GROUP BY ?rawRelated
        }
      }

      OPTIONAL {
        ?variant rdf:type ?type .
      }

      BIND(
        IF(EXISTS { ?variant ?anyP ?anyO }, 1, 0)
        AS ?knownInt
      )
    }
    GROUP BY ?cluster
    ORDER BY LCASE(
      STR(
        COALESCE(
          SAMPLE(?labels),
          MIN(STR(?variant))
        )
      )
    )
    LIMIT ${LIMIT}
  `;
  
  return selectQuery(fullQuery, signal, "Related links");
}



function appendTypeBadge(anchor, typeBinding) {
  if (!typeBinding?.value) return;

  const sup = document.createElement("sup");
  sup.className = "node-type";
  sup.textContent = typeBinding.value
    .split(" · ")
    .map(shortenIri)
    .join(" · ");

  anchor.appendChild(document.createTextNode(" "));
  anchor.appendChild(sup);
}

function appendNodeLabel(element, term, boundLabel, lookup) {
  const text = document.createTextNode(boundLabel?.value || shortenIri(term.value));
  element.appendChild(text);
  if (lookup && !boundLabel?.value && term.type === "uri") {
    lookup(term.value).then(label => {
      if (label?.value) text.textContent = label.value;
    });
  }
}

function renderRelated(bindings, lookup) {
  if (bindings.length === 0) return;

  relatedBoxEl.hidden = false;
  const items = document.createDocumentFragment();

  for (const binding of bindings) {

    const li = document.createElement("li");

    const a = document.createElement("a");

    const value = binding.related.value;

    const isInternal_old =
      Boolean(binding.relatedLabel) ||
      value.startsWith("urn:");

    const isInternal =
      FAST_MODE ||
      binding.isKnown?.value === "1" ||
      binding.isKnown?.value === "true" ||
      Boolean(binding.relatedLabel) ||
      Boolean(binding.relatedType?.value) ||
      value.startsWith("urn:");

    a.href = isInternal
      ? "#" + encodeURIComponent(value)
      : value;

    if (!isInternal) {
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    }

    appendNodeLabel(a, binding.related, binding.relatedLabel, lookup);
    appendTypeBadge(a, binding.relatedType);
    a.title = binding.related.value;

    li.appendChild(
      withCopy(a, binding.related.value)
    );

    items.appendChild(li);
  }
  relatedListEl.appendChild(items);
}



function renderSameAs(bindings, lookup) {

  if (bindings.length === 0) {
    return;
  }

  sameAsBoxEl.hidden = false;
  const items = document.createDocumentFragment();

  for (const binding of bindings) {

    const li = document.createElement("li");

    const a = document.createElement("a");

    const value = binding.same.value;

    a.href = "#" + encodeURIComponent(value);
    appendNodeLabel(a, binding.same, binding.sameLabel, lookup);

    appendTypeBadge(a, binding.sameType);
    a.title = value;

    li.appendChild(
      withCopy(a, value)
    );

    const external = document.createElement("a");
    external.href = value;
    external.target = "_blank";
    external.rel = "noopener noreferrer";
    external.textContent = " ↗";
    external.title = "Open original URI";
    external.setAttribute("aria-label", "Open original URI in new tab");

    li.appendChild(external);

    // const isInternal =
    //   Boolean(binding.sameLabel) ||
    //   value.startsWith("urn:");

    // a.href = isInternal
    //   ? "#" + encodeURIComponent(value)
    //   : value;

    // if (!isInternal) {
    //   a.target = "_blank";
    //   a.rel = "noopener noreferrer";
    // }

    // a.textContent =
    //   binding.sameLabel?.value ||
    //   shortenIri(binding.same.value);
    // appendTypeBadge(a, binding.sameType);
    // a.title = binding.same.value;

    // li.appendChild(
    //   withCopy(a, binding.same.value)
    // );

    items.appendChild(li);
  }
  sameAsListEl.appendChild(items);
}

function renderTriples(bindings, lookup) {
  const normalRows = [];
  const resourceTypes = [];

  for (const binding of bindings) {
    const p = binding.p.value;

    if (isRdfsLabel(p)) {
      resourceLabelEl.textContent = "";
      resourceLabelEl.appendChild(
        withCopy(
          document.createTextNode(binding.o.value),
          binding.o.value
        )
      );
    }

    if (isRdfType(p)) {
      resourceTypes.push(binding.o.value);

      const span = document.createElement("span");
      span.className = "resource-type";

      appendNodeLabel(span, binding.o, binding.oLabel, lookup);

      resourceTypesEl.appendChild(
        withCopy(span, binding.o.value)
      );
    }

    if (isRdfsComment(p)) {
      const pEl = document.createElement("p");
      pEl.textContent = binding.o.value;

      commentTextEl.appendChild(pEl);
      commentBoxEl.hidden = false;
    }

    if (isGeneratedAtTime(p)) {
      generatedAtEl.textContent =
        "Generated at: " + binding.o.value;

      generatedAtEl.hidden = false;
    }

    const hasPropertyFilter = PROPERTY_FILTERS.length > 0;

    if (
      !HIDDEN_PROPERTIES.has(p) &&
      (!hasPropertyFilter || PROPERTY_FILTERS.includes(p))
    ) {
      normalRows.push(binding);
    }
  }

  const uri = getUriFromLocation();
  setEditLink(uri, resourceTypes);

  if (VIEW_MODE === "html") {
    const htmlBinding = normalRows.find(
      binding =>
        binding.o?.datatype ===
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#HTML"
    );

    if (htmlBinding) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");

      td.colSpan = 2;
      td.className = "html-literal";
      td.appendChild(renderObject(htmlBinding, lookup));

      tr.appendChild(td);
      triplesEl.appendChild(tr);
      return;
    }
  }
  const rows = document.createDocumentFragment();
  for (const binding of normalRows) {
    const tr = document.createElement("tr");

    const isExternal =
      !FAST_MODE &&
      binding.o?.type === "uri" &&
      binding.isKnown?.value !== "1" &&
      binding.isKnown?.value !== "true";

    if (isExternal) {
      tr.classList.add("external-iri-row");
    }

    const propertyTd = document.createElement("td");
    propertyTd.appendChild(renderProperty(binding));

    const objectTd = document.createElement("td");
    objectTd.appendChild(renderObject(binding, lookup));

    tr.appendChild(propertyTd);
    tr.appendChild(objectTd);

    rows.appendChild(tr);
  }
  triplesEl.appendChild(rows);
}

function renderTriples_deprecated(bindings) {
  const normalRows = [];
  const resourceTypes = [];

  for (const binding of bindings) {
    const p = binding.p.value;

    if (isRdfsLabel(p)) {
      resourceLabelEl.textContent = "";
      resourceLabelEl.appendChild(
        withCopy(
          document.createTextNode(binding.o.value),
          binding.o.value
        )
      );
    }

    if (isRdfType(p)) {
      resourceTypes.push(binding.o.value);

      const span = document.createElement("span");
      span.className = "resource-type";

      span.textContent =
        binding.oLabel?.value || shortenIri(binding.o.value);

      resourceTypesEl.appendChild(
        withCopy(span, binding.o.value)
      );
    }

    if (isRdfsComment(p)) {
      const pEl = document.createElement("p");
      pEl.textContent = binding.o.value;

      commentTextEl.appendChild(pEl);
      commentBoxEl.hidden = false;
    }

    if (isGeneratedAtTime(p)) {
      generatedAtEl.textContent =
        "Generated at: " + binding.o.value;

      generatedAtEl.hidden = false;
    }

    const hasPropertyFilter = PROPERTY_FILTERS.length > 0;

    if (
      !HIDDEN_PROPERTIES.has(p) &&
      (!hasPropertyFilter || PROPERTY_FILTERS.includes(p))
    ) {
      normalRows.push(binding);
    }

  }

  const uri = getUriFromLocation();
  setEditLink(uri, resourceTypes);

  for (const binding of normalRows) {
    const tr = document.createElement("tr");

    const isExternal =
      binding.o?.type === "uri" &&
      binding.isKnown?.value !== "1" &&
      binding.isKnown?.value !== "true";

    if (isExternal) {
      tr.classList.add("external-iri-row");
    }

    const propertyTd = document.createElement("td");
    propertyTd.appendChild(renderProperty(binding));

    const objectTd = document.createElement("td");
    objectTd.appendChild(renderObject(binding));

    tr.appendChild(propertyTd);
    tr.appendChild(objectTd);

    triplesEl.appendChild(tr);
  }
}

function renderIncomingSubject(binding, lookup) {
  const link = document.createElement("a");

  link.href =
    "#" + encodeURIComponent(binding.s.value);

  appendNodeLabel(link, binding.s, binding.sLabel, lookup);

  appendTypeBadge(link, binding.sType);

  link.title = binding.s.value;

  return withCopy(link, binding.s.value);
}

function renderIncomingProperty(binding) {

  const span = document.createElement("span");

  span.textContent =
    binding.pLabel?.value ||
    shortenIri(binding.p.value);

  span.title = binding.p.value;

  return withCopy(span, binding.p.value);
}
function renderIncomingTriples(bindings, lookup) {

  const visibleBindings = bindings.filter(
    binding => !HIDDEN_PROPERTIES.has(binding.p.value)
  );
  // const visibleBindings = bindings.filter(
  //   binding =>
  //     !HIDDEN_PROPERTIES.has(binding.p.value) &&
  //     (!PROPERTY_FILTER || binding.p.value === PROPERTY_FILTER)
  // );

  if (visibleBindings.length === 0) {
    return;
  }

  incomingSectionEl.hidden = false;
  const rows = document.createDocumentFragment();

  for (const binding of visibleBindings) {

    const tr = document.createElement("tr");

    const subjectTd = document.createElement("td");
    const propertyTd = document.createElement("td");

    subjectTd.appendChild(
      renderIncomingSubject(binding, lookup)
    );

    propertyTd.appendChild(
      renderIncomingProperty(binding)
    );

    tr.appendChild(subjectTd);
    tr.appendChild(propertyTd);

    rows.appendChild(tr);
  }
  incomingTriplesEl.appendChild(rows);
}

function renderProperty(binding) {
  const span = document.createElement("span");
  span.className = "property";
  span.textContent = binding.pLabel?.value || shortenIri(binding.p.value);
  span.title = binding.p.value;
  return span;
}

function renderObject(binding, lookup) {
  const object = binding.o;

  if (object.datatype === "http://www.w3.org/1999/02/22-rdf-syntax-ns#HTML") {
    const div = document.createElement("div");
    div.className = "html-literal";
    div.innerHTML = object.value;

    return withCopy(div, object.value);
  }
if (object.type === "uri" || object.type === "bnode") {
  const isExternal =
    !FAST_MODE &&
    object.type === "uri" &&
    binding.isKnown?.value !== "1" &&
    binding.isKnown?.value !== "true";

  const wrap = document.createElement("span");
  wrap.className = "iri-object";

  const link = document.createElement("a");

  link.href = isExternal
    ? object.value
    : "#" + encodeURIComponent(object.value);

  appendNodeLabel(link, object, binding.oLabel, lookup);

  appendTypeBadge(link, binding.oType);
  link.title = object.value;

  if (isExternal) {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.classList.add("external-iri");

    const icon = document.createElement("span");
    icon.className = "external-link-icon";
    icon.textContent = " ↗";
    icon.setAttribute("aria-hidden", "true");

    link.appendChild(icon);
  }

  wrap.appendChild(withCopy(link, object.value));
  return wrap;
}

  const wrap = document.createElement("span");
  wrap.className = "literal-wrap";

  const valueSpan = document.createElement("span");
  valueSpan.className = "literal";
  valueSpan.textContent = object.value;
  wrap.appendChild(valueSpan);

  if (object["xml:lang"]) {
    const langSpan = document.createElement("span");
    langSpan.className = "datatype-badge";
    langSpan.textContent = "@" + object["xml:lang"];
    wrap.appendChild(langSpan);
  }

  if (
    object.datatype &&
    object.datatype !== "http://www.w3.org/2001/XMLSchema#string"
  ) {
    const datatypeSpan = document.createElement("span");
    datatypeSpan.className = "datatype-badge";
    datatypeSpan.textContent = shortenIri(object.datatype);
    datatypeSpan.title = object.datatype;
    wrap.appendChild(datatypeSpan);
  }

  return withCopy(wrap, object.value);
}

function shortenIri(iri) {
  const prefixed = iri
    .replace("http://www.w3.org/1999/02/22-rdf-syntax-ns#", "rdf:")
    .replace("http://www.w3.org/2000/01/rdf-schema#", "rdfs:")
    .replace("http://www.w3.org/2002/07/owl#", "owl:")
    .replace("http://www.w3.org/2001/XMLSchema#", "xsd:");

  if (prefixed !== iri) {
    return prefixed;
  }

  try {
    const url = new URL(iri);

    const host = url.hostname
      .replace(/^www\./, "")
      .split(".")[0];

    const local =
      url.hash.replace(/^#/, "") ||
      url.pathname.split("/").filter(Boolean).pop();

    return local
      ? `${host}:${local}`
      : host;

  } catch {
    return iri;
  }
}

function createQueryLink(uri) {
  const query = buildQuery(uri).trim();

  const url = new URL(QUERY_UI, window.location.href);
  url.searchParams.set("endpoint", LibraryWorkspace.linkEndpoint(ENDPOINT));
  url.searchParams.set("query", query);

  const link = document.createElement("a");
  link.href = url.toString();
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.className = "external-link-icon";
  link.textContent = "SPARQL";
  link.title = `Open the ${FAST_MODE ? "fast" : "full"} SELECT query used by the resource view`;
  link.setAttribute("aria-label", "Open SPARQL SELECT query used by the resource view");

  return link;
}

function createRdfDownloadLink(uri) {
  const link = document.createElement("a");
  const defaultTitle = "Download direct resource triples as RDF (Turtle preferred)";
  let isDownloading = false;

  link.href = "#";
  link.className = "external-link-icon";
  link.textContent = "RDF";
  link.title = defaultTitle;
  link.setAttribute("aria-label", "Download resource as RDF");

  link.addEventListener("click", async (event) => {
    event.preventDefault();

    if (isDownloading) return;

    isDownloading = true;
    link.setAttribute("aria-disabled", "true");
    link.textContent = "RDF …";

    try {
      await downloadResourceRdf(uri);
      link.textContent = "RDF ✓";
    } catch (error) {
      console.error(error);
      link.textContent = "RDF error";
      link.title = error instanceof Error
        ? error.message
        : "RDF download failed";
    } finally {
      setTimeout(() => {
        isDownloading = false;
        link.removeAttribute("aria-disabled");
        link.textContent = "RDF";
        link.title = defaultTitle;
      }, 1500);
    }
  });

  return link;
}

async function downloadResourceRdf(uri) {
  const query = buildRdfQuery(uri);
  LibraryWorkspace.record("RDF download", "POST " + ENDPOINT, query);
  const response = await fetch(ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/sparql-query",
      "Accept": "text/turtle, application/n-triples;q=0.9, application/rdf+xml;q=0.8"
    },
    body: query
  });

  if (!response.ok) {
    throw new Error(`RDF download failed: endpoint returned ${response.status}`);
  }

  const contentType = response.headers.get("content-type") || "text/turtle";

  if (contentType.includes("application/sparql-results")) {
    throw new Error("RDF download failed: endpoint returned a SELECT result");
  }

  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");

  link.href = objectUrl;
  link.download = createRdfFileName(uri, contentType);
  link.hidden = true;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

function buildRdfQuery(uri) {
  const escapedUri = escapeSparqlIri(uri);

  return `
    CONSTRUCT {
      <${escapedUri}> ?p ?o .
    }
    WHERE {
      <${escapedUri}> ?p ?o .
    }
  `.trim();
}

function createRdfFileName(uri, contentType) {
  const mimeType = contentType.split(";", 1)[0].trim().toLowerCase();
  const extensionByMimeType = {
    "application/ld+json": "jsonld",
    "application/n-quads": "nq",
    "application/n-triples": "nt",
    "application/rdf+xml": "rdf",
    "application/trig": "trig",
    "text/turtle": "ttl"
  };
  const extension = extensionByMimeType[mimeType] || "ttl";
  let stem = "resource";

  try {
    const url = new URL(uri);
    stem = url.hash.replace(/^#/, "")
      || url.pathname.split("/").filter(Boolean).pop()
      || url.hostname
      || stem;
  } catch {
    stem = uri.split(/[\/#:]/).filter(Boolean).pop() || stem;
  }

  try {
    stem = decodeURIComponent(stem);
  } catch {
    // Keep the encoded value when the IRI contains a malformed escape sequence.
  }

  stem = stem.replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    || "resource";

  return `${stem}.${extension}`;
}

function escapeSparqlIri(iri) {
  return iri.replace(/[<>"{}|^`\\]/g, encodeURIComponent);
}

function showStatus(message) {
  statusEl.textContent = message;
  statusEl.hidden = false;
}

function hideStatus() {
  statusEl.hidden = true;
}
