// const ENDPOINT = "http://localhost:7879/query";
const DEFAULT_ENDPOINT = "/sparql/query";

const DEFAULT_ONTOLOGY_GRAPH = "http://www.zotero.org/namespaces/export";
// const ONTOLOGY_GRAPH = null;

const params = new URLSearchParams(location.search);

const ENDPOINT =
  params.get("endpoint")
  || DEFAULT_ENDPOINT;

const ONTOLOGY_GRAPH =
  params.get("ontology")
  || DEFAULT_ONTOLOGY_GRAPH;

console.log("SPARQL endpoint:", ENDPOINT);

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


const LANGUAGE = "en";
const LIMIT = 1000;

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

window.addEventListener("hashchange", () => {
  loadCurrentResource();
});

loadCurrentResource();

async function loadCurrentResource() {
  const uri = getUriFromLocation();

  triplesEl.innerHTML = "";
  contentEl.hidden = true;
    resourceLabelEl.textContent = "";
    resourceTypesEl.innerHTML = "";
    commentTextEl.innerHTML = "";
    commentBoxEl.hidden = true;

    sameAsListEl.innerHTML = "";
    sameAsBoxEl.hidden = true;
    relatedListEl.innerHTML = "";
    relatedBoxEl.hidden = true;
    generatedAtEl.hidden = true;
    generatedAtEl.textContent = "";

    incomingTriplesEl.innerHTML = "";
    incomingSectionEl.hidden = true;
  if (!uri) {
    showStatus("No URI given, attacht like so in URL: #https://example.org/resource/123");
    return;
  }

  resourceUriEl.innerHTML = "";

  const span = document.createElement("span");
  span.className = "resource-uri-text";
  span.textContent = uri;

  resourceUriEl.appendChild(withCopy(span, uri));
  showStatus("Loading Resource …");

  try {
    const resolvedUri = await resolveUri(uri);

    console.log("Requested URI:", uri);
    console.log("Resolved URI:", resolvedUri);

    const data = await queryResource(resolvedUri);
    console.log(
      "rdfs:label bindings",
      data.results.bindings.filter(
        b => b.p.value === "http://www.w3.org/2000/01/rdf-schema#label"
      )
    );
    const incomingData = await queryIncoming(resolvedUri);
    const sameAsData = await querySameAs(resolvedUri, uri);
    const relatedData = await queryRelated(resolvedUri);

    console.log("Endpoint:", ENDPOINT);
    console.log("Resolved endpoint:", new URL(ENDPOINT, window.location.href).href);
    if (data.results.bindings.length === 0) {
      showStatus("No triples!");
      return;
    }

    renderTriples(data.results.bindings);
    renderIncomingTriples(incomingData.results.bindings);
    renderSameAs(sameAsData.results.bindings);
    renderRelated(relatedData.results.bindings);
    hideStatus();
    contentEl.hidden = false;
  } catch (error) {
    console.error(error);
    showStatus("Error");
  }
}

function getUriFromLocation() {
  if (location.hash.length > 1) {
    return decodeURIComponent(location.hash.slice(1));
  }

  return new URLSearchParams(location.search).get("uri");
}

async function queryResource(uri) {
  const query = buildQuery(uri);

  const response = await fetch(ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/sparql-query",
      "Accept": "application/sparql-results+json"
    },
    body: query
  });

  if (!response.ok) {
    throw new Error(`SPARQL endpoint returned ${response.status}`);
  }

  return response.json();
}

async function queryIncoming(uri) {

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

  const response = await fetch(ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/sparql-query",
      "Accept": "application/sparql-results+json"
    },
    body: query
  });

  if (!response.ok) {
    throw new Error(`SPARQL endpoint returned ${response.status}`);
  }

  return response.json();
}

async function querySameAs(uri, requestedUri) {

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

  const response = await fetch(ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/sparql-query",
      "Accept": "application/sparql-results+json"
    },
    body: query
  });

  if (!response.ok) {
    throw new Error(`SPARQL endpoint returned ${response.status}`);
  }

  return response.json();
}

function buildQuery(uri) {
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

async function resolveUri(uri) {

  const query = `
    PREFIX owl: <http://www.w3.org/2002/07/owl#>

    SELECT ?canonical WHERE {

      ?canonical owl:sameAs* <${escapeSparqlIri(uri)}> .

      ?canonical ?p ?o .
    }
    ORDER BY IF(?canonical = <${escapeSparqlIri(uri)}>, 0, 1)
    LIMIT 1
  `;

  const response = await fetch(ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/sparql-query",
      "Accept": "application/sparql-results+json"
    },
    body: query
  });

  if (!response.ok) {
    throw new Error(`SPARQL endpoint returned ${response.status}`);
  }

  const data = await response.json();

  return data.results.bindings[0]?.canonical?.value || uri;
}

async function queryRelated(uri) {

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
  const query = `
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
  
  const response = await fetch(ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/sparql-query",
      "Accept": "application/sparql-results+json"
    },
    body: query
  });

  if (!response.ok) {
    throw new Error(`SPARQL endpoint returned ${response.status}`);
  }

  return response.json();
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

function renderRelated(bindings) {
  if (bindings.length === 0) return;

  relatedBoxEl.hidden = false;

  for (const binding of bindings) {

    const li = document.createElement("li");

    const a = document.createElement("a");

    const value = binding.related.value;

    const isInternal_old =
      Boolean(binding.relatedLabel) ||
      value.startsWith("urn:");

    const isInternal =
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

    a.textContent =
      binding.relatedLabel?.value ||
      shortenIri(binding.related.value);
    appendTypeBadge(a, binding.relatedType);
    a.title = binding.related.value;

    li.appendChild(
      withCopy(a, binding.related.value)
    );

    relatedListEl.appendChild(li);
  }
}



function renderSameAs(bindings) {

  if (bindings.length === 0) {
    return;
  }

  sameAsBoxEl.hidden = false;

  for (const binding of bindings) {

    const li = document.createElement("li");

    const a = document.createElement("a");

    const value = binding.same.value;

    a.href = "#" + encodeURIComponent(value);
    a.textContent =
      binding.sameLabel?.value ||
      shortenIri(value);

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

    sameAsListEl.appendChild(li);
  }
}

function renderTriples(bindings) {

  const normalRows = [];

  for (const binding of bindings) {

    const p = binding.p.value;

    if (isRdfsLabel(p)) {
        resourceLabelEl.textContent = "";
        resourceLabelEl.appendChild(withCopy(
        document.createTextNode(binding.o.value),
        binding.o.value
        ));
    }

    if (isRdfType(p)) {

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

    if (!HIDDEN_PROPERTIES.has(p)) {
      normalRows.push(binding);
    }
  }

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
function renderIncomingSubject(binding) {
  const link = document.createElement("a");

  link.href =
    "#" + encodeURIComponent(binding.s.value);

  link.textContent =
    binding.sLabel?.value ||
    shortenIri(binding.s.value);

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
function renderIncomingTriples(bindings) {

  const visibleBindings = bindings.filter(
    binding => !HIDDEN_PROPERTIES.has(binding.p.value)
  );


  console.log("Incoming bindings:", visibleBindings);
  if (visibleBindings.length === 0) {
    return;
  }

  incomingSectionEl.hidden = false;

  for (const binding of visibleBindings) {

    const tr = document.createElement("tr");

    const subjectTd = document.createElement("td");
    const propertyTd = document.createElement("td");

    subjectTd.appendChild(
      renderIncomingSubject(binding)
    );

    propertyTd.appendChild(
      renderIncomingProperty(binding)
    );

    tr.appendChild(subjectTd);
    tr.appendChild(propertyTd);

    incomingTriplesEl.appendChild(tr);
  }
}

function renderProperty(binding) {
  const span = document.createElement("span");
  span.className = "property";
  span.textContent = binding.pLabel?.value || shortenIri(binding.p.value);
  span.title = binding.p.value;
  return span;
}

function renderObject(binding) {
  const object = binding.o;

  if (object.datatype === "http://www.w3.org/1999/02/22-rdf-syntax-ns#HTML") {
    const div = document.createElement("div");
    div.className = "html-literal";
    div.innerHTML = object.value;

    return withCopy(div, object.value);
  }
if (object.type === "uri" || object.type === "bnode") {
  const isExternal =
    object.type === "uri" &&
    binding.isKnown?.value !== "1" &&
    binding.isKnown?.value !== "true";

  const wrap = document.createElement("span");
  wrap.className = "iri-object";

  const link = document.createElement("a");

  link.href = isExternal
    ? object.value
    : "#" + encodeURIComponent(object.value);

  link.textContent =
    binding.oLabel?.value || shortenIri(object.value);

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