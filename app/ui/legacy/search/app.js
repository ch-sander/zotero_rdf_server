const OPENSEARCH_HOST = "/plugin/fts/proxy/search";

const DEFAULT_INDEX_NAME = "scigma-main-corpus";

let activeIndexName =
  localStorage.getItem("ocrSearchIndex") || DEFAULT_INDEX_NAME;

let searchInstance = null;
let useMlt = false;

// const SEARCH_ATTRIBUTES_OLD = [
//   { field: "text", weight: 5 },
//   { field: "label", weight: 3 },
//   { field: "meta.file_label", weight: 3 },
//   { field: "meta.parent_creators", weight: 2 },
//   { field: "meta.parent_tags", weight: 2 },
//   { field: "meta.library_label", weight: 2 },
//   { field: "meta.file", weight: 1 }
// ];

// const RESULT_ATTRIBUTES_OLD = [
//   "doc_id",
//   "label",
//   "source",
//   "page",
//   "text",
//   "ingest_ts",
//   "meta.file",
//   "meta.file_label",
//   "meta.library",
//   "meta.library_label",
//   "meta.link_type",
//   "meta.parent",
//   "meta.parent_creators",
//   "meta.parent_key",
//   "meta.parent_tags",
//   "meta.date"
// ];

const HIGHLIGHT_ATTRIBUTES = ["label", "text"];
const SNIPPET_ATTRIBUTES = ["text"];
const HIT_BLACKLIST = new Set(["vector"]);

const FACET_BLACKLIST = new Set([
  "doc_id",
  "label",
  "vector",
  "meta.parent",
  "meta.file_label",
  "meta.library",
  "text",
  "source",
  "ingest_ts",
  "meta.file",
  "concepts.start",
  "concepts.end",
  "meta.link_type",
]);

const FACET_PRIORITY = [
  "source",
  "label",
  "meta.library",
  "meta.library_label",
  "meta.link_type",
  "meta.parent_creators",
  "meta.parent_tags",
  "meta.date",
  "page"
];

const NUMERIC_TYPES = new Set([
  "integer",
  "long",
  "short",
  "byte",
  "float",
  "double",
  "half_float",
  "scaled_float"
]);

const LINK_CONFIG = {
  doc: {
    directKey: "viewer",
    template: "/plugin/fts/view/{os_doc_id}",
    map: (item) => ({
      os_doc_id: item.objectID || item.doc_id || item.id
    })
  }
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function getNestedValue(obj, path) {
  return path.split(".").reduce((acc, part) => acc?.[part], obj);
}

function flattenProperties(properties, prefix = "") {
  const out = [];

  for (const [name, def] of Object.entries(properties || {})) {
    const path = prefix ? `${prefix}.${name}` : name;

    if (def.type) {
      out.push({ field: path, type: def.type });
    }

    if (def.properties) {
      out.push(...flattenProperties(def.properties, path));
    }
  }

  return out;
}

function toAttributeName(field) {
  return field.replace(/\./g, "_");
}

function prettyLabel(field) {
  return field
    .split(".")
    .slice(-1)[0]
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function fieldPriority(field) {
  const idx = FACET_PRIORITY.indexOf(field);
  return idx === -1 ? 999 : idx;
}

function shouldUseAsFacet(fieldInfo) {
  if (FACET_BLACKLIST.has(fieldInfo.field)) return false;
  if (fieldInfo.type === "keyword") return true;
  if (NUMERIC_TYPES.has(fieldInfo.type)) return true;
  return false;
}

function buildFacetAttributes(fields) {
  return fields
    .filter(shouldUseAsFacet)
    .map((f) => ({
      attribute: toAttributeName(f.field),
      field: f.field,
      type: f.type === "keyword" ? "string" : "numeric",
      label: prettyLabel(f.field),
      originalType: f.type
    }))
    .sort((a, b) => {
      const pa = fieldPriority(a.field);
      const pb = fieldPriority(b.field);
      if (pa !== pb) return pa - pb;
      return a.field.localeCompare(b.field);
    });
}

async function loadMapping(indexName = activeIndexName) {
  const res = await fetch(`${OPENSEARCH_HOST}/${indexName}/_mapping`);
  if (!res.ok) {
    throw new Error(`Failed to load mapping: ${res.status} ${res.statusText}`);
  }
  return res.json();
}

function createFacetContainers(facets) {
  const root = document.getElementById("dynamic-facets");
  root.innerHTML = "";

  for (const facet of facets) {
    const wrapper = document.createElement("section");
    wrapper.className = "facet-block";

    const title = document.createElement("h4");
    title.textContent = facet.label;

    const container = document.createElement("div");
    container.id = `facet-${facet.attribute}`;

    wrapper.appendChild(title);
    wrapper.appendChild(container);
    root.appendChild(wrapper);
  }
}

function buildFacetWidgets(facets) {
  const widgets = [];

  for (const facet of facets) {
    if (facet.type === "string") {
      widgets.push(
        instantsearch.widgets.refinementList({
          container: `#facet-${facet.attribute}`,
          attribute: facet.attribute,
          searchable: true,
          searchablePlaceholder: `Search ${facet.label}...`,
          showMore: true,
          limit: 8,
          sortBy: ["count:desc", "name:asc"]
        })
      );
    } else if (facet.type === "numeric") {
      widgets.push(
        instantsearch.widgets.rangeInput({
          container: `#facet-${facet.attribute}`,
          attribute: facet.attribute
        })
      );
    }
  }

  return widgets;
}

function buildUrl(type, item) {
  const cfg = LINK_CONFIG[type];
  if (!cfg || !item) return "";

  if (cfg.directKey && item[cfg.directKey]) {
    return item[cfg.directKey];
  }

  const vars = cfg.map ? cfg.map(item) : item;

  return cfg.template.replace(/\{(\w+)\}/g, (_, key) =>
    encodeURIComponent(vars[key] ?? "")
  );
}


function buildHitsWidget() {
  return instantsearch.widgets.hits({
    container: "#hits",
    templates: {
      empty(results, { html }) {
        return html`<div>No results for <strong>${results.query}</strong>.</div>`;
      },
      item(hit, { html, components }) {
        const label = hit.label || hit.meta?.file_label || hit.meta?.file || "Untitled";
        const source = hit.source || "-";
        const page = hit.page ?? "-";
        const parent = hit.meta?.parent || "-";
        const creators = hit.meta?.parent_creators || "-";
        const tags = hit.meta?.parent_tags || "-";
        const date = hit.meta?.date || "-";
        const snippet = components.Snippet({ attribute: "text", hit });
        const raw = hit.text ? hit.text.slice(0, 10000) : "";
        const url = buildUrl("doc", hit);

        const badge = (name, value) => {
          const text = Array.isArray(value)
            ? value.join(", ")
            : String(value ?? "-");

          return html`
            <button
              type="button"
              class="badge copyable"
              title="Copy ${name}"
              onClick=${async (event) => {
                const button = event.currentTarget;
                const feedback = button.querySelector(".copy-feedback");

                clearTimeout(button.copyTimer);

                try {
                  await navigator.clipboard.writeText(text);
                  feedback.textContent = " ✓ Copied";
                } catch (error) {
                  feedback.textContent = " – Failed to copy!";
                  console.error(error);
                }

                button.copyTimer = setTimeout(() => {
                  feedback.textContent = "";
                }, 2000);
              }}
            >
              ${name}: ${text}
              <span class="copy-feedback" aria-live="polite"></span>
            </button>
          `;
        };

        return html`
          <div class="hit">
            <h2>${
                url
                  ? html`<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`
                  : label
              }</h2>

            <div class="meta">
              ${badge("ID", hit.objectID)}
              ${badge("Parent", parent)}
              ${badge("Source", source)}
              ${badge("Page", page)}
              ${badge("Creators", creators)}
              ${badge("Date", date)}
              ${badge("Tags", tags)}
            </div>

            <div class="snippet">
              ${snippet}
            </div>

            <details>
              <summary>Show more</summary>
              <div class="snippet more">${raw}</div>
            </details>
          </div>
        `;
      }
    }
  });
}

function buildResultAttributes(fields) {
  return fields
    .map((f) => f.field)
    .filter((field) => !HIT_BLACKLIST.has(field));
}

function initSearchMode(search, initialQuery) {
  document.getElementById("searchbox").innerHTML = `
    <div style="display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; margin-bottom: 0.5rem">
      <label>
        <input id="mlt-toggle" type="checkbox"> More Like This (MLT)
      </label>
      <button id="show-opensearch-query" type="button" disabled
        aria-haspopup="dialog" aria-controls="opensearch-query-dialog"
        title="Show full OpenSearch query"
        style="font-size: 0.8rem; padding: 0.2rem 0.5rem; cursor: pointer">Query</button>
    </div>
    <div id="term-searchbox"></div>
    <form id="mlt-searchbox" hidden>
      <textarea id="mlt-text" class="ais-SearchBox-input" rows="6"
        aria-label="Text for similarity search"
        placeholder="Paste a text to find similar OCR content..."
        style="height: auto; padding: 0.75rem; resize: vertical"></textarea>
      <button type="submit">Search similar text</button>
      <button type="reset">Clear</button>
    </form>
  `;

  const toggle = document.getElementById("mlt-toggle");
  const termBox = document.getElementById("term-searchbox");
  const form = document.getElementById("mlt-searchbox");
  const textarea = document.getElementById("mlt-text");
  const updateMode = () => {
    termBox.hidden = useMlt;
    form.hidden = !useMlt;
  };

  toggle.checked = useMlt;
  textarea.value = initialQuery;
  updateMode();

  toggle.addEventListener("change", () => {
    const query = useMlt ? textarea.value : search.helper.state.query || "";
    useMlt = toggle.checked;
    textarea.value = query;
    updateMode();
    search.helper.clearCache().setQuery(query).setPage(0).search();
    (useMlt ? textarea : termBox.querySelector("input")).focus();
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    search.helper.setQuery(textarea.value).setPage(0).search();
  });

  form.addEventListener("reset", (event) => {
    event.preventDefault();
    textarea.value = "";
    search.helper.setQuery("").setPage(0).search();
    textarea.focus();
  });
}

function initQueryInspector() {
  document.getElementById("searchbox").insertAdjacentHTML("beforeend", `
    <style>
      #opensearch-query-dialog {
        box-sizing: border-box;
        width: min(900px, calc(100vw - 2rem));
        max-height: calc(100dvh - 2rem);
        overflow: auto;
        padding: 1.25rem;
        border: 1px solid #cbd5e1;
        border-radius: 0.65rem;
        background: #fff;
        color: #0f172a;
      }
      #opensearch-query-dialog::backdrop { background: rgb(0 0 0 / 45%); }
      #opensearch-query-dialog .query-dialog-header,
      #opensearch-query-dialog .query-dialog-footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 0.75rem;
      }
      #opensearch-query-dialog h2 { margin: 0; font-size: 1.1rem; }
      #opensearch-query-dialog button { padding: 0.35rem 0.65rem; cursor: pointer; }
      #opensearch-query-index { margin: 0.75rem 0; overflow-wrap: anywhere; }
      #opensearch-query-picker { display: block; margin-bottom: 0.75rem; }
      #opensearch-query-picker[hidden] { display: none; }
      #opensearch-query-text {
        box-sizing: border-box;
        width: 100%;
        height: 55vh;
        max-height: 32rem;
        margin-bottom: 0.75rem;
        padding: 0.75rem;
        border: 1px solid #cbd5e1;
        border-radius: 0.35rem;
        background: #f8fafc;
        color: #0f172a;
        font: 0.8rem/1.5 ui-monospace, SFMono-Regular, Consolas, monospace;
        resize: vertical;
      }
    </style>
    <dialog id="opensearch-query-dialog" aria-labelledby="opensearch-query-title">
      <div class="query-dialog-header">
        <h2 id="opensearch-query-title">OpenSearch query</h2>
        <button id="close-opensearch-query" type="button" aria-label="Close">✕</button>
      </div>
      <p id="opensearch-query-index"></p>
      <label id="opensearch-query-picker" hidden>
        Request: <select id="opensearch-query-select"></select>
      </label>
      <textarea id="opensearch-query-text" readonly spellcheck="false" wrap="off"
        aria-label="Full OpenSearch query"></textarea>
      <div class="query-dialog-footer">
        <span id="opensearch-query-feedback" role="status"></span>
        <button id="copy-opensearch-query" type="button" disabled>Copy</button>
      </div>
    </dialog>
  `);

  const openButton = document.getElementById("show-opensearch-query");
  const dialog = document.getElementById("opensearch-query-dialog");
  const picker = document.getElementById("opensearch-query-picker");
  const select = document.getElementById("opensearch-query-select");
  const index = document.getElementById("opensearch-query-index");
  const output = document.getElementById("opensearch-query-text");
  const copyButton = document.getElementById("copy-opensearch-query");
  const feedback = document.getElementById("opensearch-query-feedback");
  let queries = [];

  const render = () => {
    const query = queries[Number(select.value) || 0];
    index.textContent = query ? `Index: ${query.indexName}` : "";
    output.value = query?.body || "No search request available yet.";
    copyButton.disabled = !query;
    feedback.textContent = "";
  };

  openButton.addEventListener("click", () => {
    feedback.textContent = "";
    if (!dialog.open) dialog.showModal();
  });
  document.getElementById("close-opensearch-query")
    .addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    const rect = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < rect.left ||
        event.clientX > rect.right || event.clientY < rect.top ||
        event.clientY > rect.bottom)) dialog.close();
  });
  select.addEventListener("change", render);
  copyButton.addEventListener("click", async () => {
    const text = output.value;
    const copyFallback = () => {
      output.focus();
      output.select();
      if (!document.execCommand("copy")) throw new Error("Copy failed");
    };
    try {
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        if (output.value !== text) return;
        copyFallback();
      }
      if (output.value === text) feedback.textContent = "Copied.";
    } catch {
      feedback.textContent = "Copy failed. Press Ctrl/Cmd+C to copy the selected text.";
    }
  });

  render();
  return (searchRequests) => {
    // Snapshot the final bodies, including facet filters and aggregations.
    queries = searchRequests.map((sr) => ({
      indexName: sr.indexName,
      body: JSON.stringify(sr.body, null, 2)
    }));
    select.replaceChildren(...queries.map((query, i) => {
      const option = document.createElement("option");
      option.value = String(i);
      option.textContent = `Request ${i + 1} of ${queries.length}`;
      return option;
    }));
    picker.hidden = queries.length < 2;
    openButton.disabled = queries.length === 0;
    render();
  };
}

async function init(indexName = activeIndexName) {
  try {
    activeIndexName = indexName;
    const mapping = await loadMapping();
    const properties = mapping?.[indexName]?.mappings?.properties || {};
    const flatFields = flattenProperties(properties);
    const dynamicFacets = buildFacetAttributes(flatFields);
    const dynamicResultAttributes = buildResultAttributes(flatFields);

    searchInstance?.dispose();
    createFacetContainers(dynamicFacets);

    const sk = new Searchkit({
      connection: {
        host: OPENSEARCH_HOST
      },
      search_settings: {
        search_attributes: ["text"],
        result_attributes: dynamicResultAttributes,
        highlight_attributes: HIGHLIGHT_ATTRIBUTES,
        snippet_attributes: SNIPPET_ATTRIBUTES,
        facet_attributes: dynamicFacets.map((f) => ({
          attribute: f.attribute,
          field: f.field,
          type: f.type
        }))
      }
    });

let displayOpenSearchQueries = () => {};
const createSearchClient = (captureQueries = () => {}) => SearchkitInstantsearchClient(sk, {
  getQuery(query) {
    const q = (query || "").trim();

    if (!q) {
      return false;
    }

    if (useMlt) {
      return [{
        more_like_this: {
          fields: ["text"],
          like: q,
          min_term_freq: 1,
          min_doc_freq: 1,
          max_query_terms: 25,
          minimum_should_match: "30%"
        }
      }];
    }

    const buildTermQuery = (term) => {
      const t = term.trim();

      if (!t) {
        return null;
      }

      const isPhrase = /\s+/.test(t);

      const should = [
        {
          match_phrase: {
            text: {
              query: t,
              slop: 2,
              boost: 6
            }
          }
        },

        {
          match_phrase_prefix: {
            text: {
              query: t,
              max_expansions: 50,
              boost: 2
            }
          }
        }
      ];

      if (!isPhrase) {
        should.push({
          match: {
            text: {
              query: t,
              fuzziness: 2,
              prefix_length: 1,
              max_expansions: 50,
              boost: 1
            }
          }
        });
      }

      return {
        bool: {
          should,
          minimum_should_match: 1
        }
      };
    };

    const orGroups = q
      .split(/\s*(?:,|;|\bOR\b)\s*/i)
      .map(v => v.trim())
      .filter(Boolean);

    const queryDsl = [
      {
        bool: {
          should: orGroups.map((group) => {

          const andTerms = group
            .split(/\s*(?:\+|\bAND\b)\s*/i)
            .map(v => v.trim())
            .filter(Boolean);

            if (andTerms.length === 1) {
              return buildTermQuery(andTerms[0]);
            }

            return {
              bool: {
                must: andTerms
                  .map(buildTermQuery)
                  .filter(Boolean)
              }
            };
          }),

          minimum_should_match: 1
        }
      }
    ];

    console.log("Search input:", query);
    return queryDsl;
  },
  hooks: {
    beforeSearch: async (searchRequests) => {
      searchRequests.forEach((sr, i) => {
        console.log(`Final OpenSearch Query ${i}`);
        console.log(JSON.stringify(sr.body, null, 2));
      });
      captureQueries(searchRequests);

      return searchRequests;
    }
  }
});

    let activeSearchClient = createSearchClient();
    const queryClients = new Map();
    let visibleQueryRequest = null;
    const searchClient = {
      search: (requests) => {
        const key = JSON.stringify(requests);
        let entry = queryClients.get(key);
        if (!entry) {
          entry = { queries: [] };
          // Keep each cached search paired with its final OpenSearch bodies.
          entry.client = createSearchClient((searchRequests) => {
            entry.queries = searchRequests.map((sr) => ({
              indexName: sr.indexName,
              body: JSON.parse(JSON.stringify(sr.body))
            }));
            if (visibleQueryRequest === entry) displayOpenSearchQueries(entry.queries);
          });
          queryClients.set(key, entry);
          if (queryClients.size > 50) queryClients.delete(queryClients.keys().next().value);
        }
        visibleQueryRequest = entry;
        displayOpenSearchQueries(entry.queries);
        return entry.client.search(requests);
      },
      searchForFacetValues: (requests) => activeSearchClient.searchForFacetValues(requests),
      clearCache: () => {
        // Isolate late responses from searches started before a mode switch.
        activeSearchClient = createSearchClient();
        queryClients.clear();
        visibleQueryRequest = null;
        displayOpenSearchQueries([]);
        return Promise.resolve();
      }
    };

    const params = new URLSearchParams(window.location.search);
    const initialQuery = params.get("q") || "";

    const search = instantsearch({
      indexName,
      searchClient,
      initialUiState: {
        [indexName]: {
          query: initialQuery
        }
      }
    });

    initSearchMode(search, initialQuery);
    displayOpenSearchQueries = initQueryInspector();
    search.addWidgets([
      instantsearch.widgets.searchBox({
        container: "#term-searchbox",
        placeholder: "Search OCR content...",
        showReset: true,
        showSubmit: true,
        showLoadingIndicator: true
      }),

      instantsearch.widgets.stats({
        container: "#stats",
        templates: {
          text(data, { html }) {
            return html`${data.nbHits} results found in ${data.processingTimeMS} ms`;
          }
        }
      }),

      ...buildFacetWidgets(dynamicFacets),

      instantsearch.widgets.clearRefinements({
        container: "#clear",
        templates: {
          resetLabel: "Clear all filters"
        }
      }),

      buildHitsWidget(),

      instantsearch.widgets.pagination({
        container: "#pagination"
      })
    ]);

    searchInstance = search;
    search.start();
  } catch (err) {
    console.error(err);
    document.getElementById("hits").innerHTML = `
      <div class="hit">
        <h2>Initialization error</h2>
        <div class="snippet">${escapeHtml(err.message)}</div>
      </div>
    `;
  }
}

function initIndexSelector() {
  const select = document.getElementById("index-select");
  if (!select) return;

  select.value = activeIndexName;

  select.addEventListener("change", () => {
    const nextIndex = select.value;
    localStorage.setItem("ocrSearchIndex", nextIndex);
    init(nextIndex);
  });
}

initIndexSelector();
init(activeIndexName);

/*     const searchClient = SearchkitInstantsearchClient(sk, {
    getQuery(query, search_attributes) {
        const fields = search_attributes.map((attr) =>
        typeof attr === "string"
            ? attr
            : `${attr.field}${attr.weight ? `^${attr.weight}` : ""}`
        );

        return {
        simple_query_string: {
            query,
            fields,
            default_operator: "and"
        }
        };
    }
    }); */

            // return [
        // {
        //     bool: {
        //     should: parts.flatMap((part) => [
        //         {
        //         match_phrase: {
        //             text: {
        //             query: part,
        //             slop: 2,
        //             boost: 4
        //             }
        //         }
        //         },
        //         {
        //         match_phrase_prefix: {
        //             text: {
        //             query: part,
        //             max_expansions: 50,
        //             boost: 2
        //             }
        //         }
        //         },
        //         {
        //         match: {
        //             text: {
        //             query: part,
        //             fuzziness: 2,
        //             prefix_length: 1,
        //             max_expansions: 50,
        //             boost: 1
        //             }
        //         }
        //         }
        //     ]),
        //     minimum_should_match: 1
        //     }
        // }
        // ];
