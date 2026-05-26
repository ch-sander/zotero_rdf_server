const ENDPOINT = "http://localhost:7879/query";
// const ENDPOINT = "/sparql/query";

const ONTOLOGY_GRAPH = "http://www.zotero.org/namespaces/export";
// const ONTOLOGY_GRAPH = null;

const HIDDEN_PROPERTIES = new Set([
  "http://www.zotero.org/namespaces/export#version",
  "http://www.w3.org/2000/01/rdf-schema#label",
  "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
  "http://www.w3.org/2000/01/rdf-schema#comment",
  "http://www.w3.org/2002/07/owl#sameAs",
  "http://www.w3.org/ns/prov#generatedAtTime"
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
  showStatus("Loading Ressource …");

  try {
    const data = await queryResource(uri);
    const incomingData = await queryIncoming(uri);
    console.log("Endpoint:", ENDPOINT);
    console.log("Resolved endpoint:", new URL(ENDPOINT, window.location.href).href);
    if (data.results.bindings.length === 0) {
      showStatus("No triples!");
      return;
    }

    renderTriples(data.results.bindings);
    renderIncomingTriples(incomingData.results.bindings);
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

  const query = `
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

    SELECT ?s ?sLabel ?p ?pLabel WHERE {

      ?s ?p <${escapeSparqlIri(uri)}> .

      OPTIONAL {
        ?s rdfs:label ?sLabel .
        FILTER(lang(?sLabel) = "${LANGUAGE}" || lang(?sLabel) = "")
      }

      OPTIONAL {
        ?p rdfs:label ?pLabel .
        FILTER(lang(?pLabel) = "${LANGUAGE}" || lang(?pLabel) = "")
      }
    }

    ORDER BY ?pLabel ?sLabel
    LIMIT 200
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
          ?p rdfs:label ?pLabel .
          FILTER(lang(?pLabel) = "${LANGUAGE}" || lang(?pLabel) = "")
        }
      }
    `
    : `
      OPTIONAL {
        ?p rdfs:label ?pLabel .
        FILTER(lang(?pLabel) = "${LANGUAGE}" || lang(?pLabel) = "")
      }
    `;

  return `
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

    SELECT ?p ?pLabel ?o ?oLabel WHERE {
      <${escapeSparqlIri(uri)}> ?p ?o .

      ${propertyLabelBlock}

      OPTIONAL {
        ?o rdfs:label ?oLabel .
        FILTER(lang(?oLabel) = "${LANGUAGE}" || lang(?oLabel) = "")
      }
    }
    ORDER BY LCASE(STR(COALESCE(?pLabel, ?p))) LCASE(STR(COALESCE(?oLabel, ?o)))
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

function isOwlSameAs(p) {
  return p === "http://www.w3.org/2002/07/owl#sameAs";
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

    if (isOwlSameAs(p)) {

      const li = document.createElement("li");

      const a = document.createElement("a");
      a.href = binding.o.value;
      a.textContent =
        binding.oLabel?.value || binding.o.value;

      a.target = "_blank";
      a.rel = "noopener noreferrer";

      li.appendChild(a);

      sameAsListEl.appendChild(li);
      sameAsBoxEl.hidden = false;
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
  console.log("Incoming bindings:", bindings);
  if (bindings.length === 0) {
    return;
  }

  incomingSectionEl.hidden = false;

  for (const binding of bindings) {

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
  if (object.type === "uri") {
    const link = document.createElement("a");
    link.href = "#" + encodeURIComponent(object.value);
    link.textContent = binding.oLabel?.value || shortenIri(object.value);
    link.title = object.value;

    return withCopy(link, object.value);
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
  return iri
    .replace("http://www.w3.org/1999/02/22-rdf-syntax-ns#", "rdf:")
    .replace("http://www.w3.org/2000/01/rdf-schema#", "rdfs:")
    .replace("http://www.w3.org/2002/07/owl#", "owl:")
    .replace("http://www.w3.org/2001/XMLSchema#", "xsd:")
    .replace(/^https?:\/\/[^/#]+[\/#]/, "");
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