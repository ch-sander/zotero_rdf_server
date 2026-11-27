const OPENSEARCH_HOST = "/plugin/fts/proxy/search";
const IS_PRODUCTION = window.location.hostname === "app.scigma.de";

document.documentElement.classList.toggle(
  "scigma-production",
  IS_PRODUCTION
);

const DEFAULT_INDEX_NAME = "scigma-main-corpus";

// let activeIndexName =
//   localStorage.getItem("ocrSearchIndex") || DEFAULT_INDEX_NAME;

let searchInstance = null;
let initVersion = 0;
let useMlt = false;
let useLucene = false;
let activeIndexName = IS_PRODUCTION
  ? DEFAULT_INDEX_NAME
  : localStorage.getItem("ocrSearchIndex") || DEFAULT_INDEX_NAME;

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
  const url = `${OPENSEARCH_HOST}/${indexName}/_mapping`;
  LibraryWorkspace.record("Index mapping", "GET", new URL(url, document.baseURI).href);
  const res = await fetch(url);
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

    const title = document.createElement("h3");
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
    <div id="term-searchbox"></div>

    <form id="mlt-searchbox" hidden>
      <textarea id="mlt-text"
        class="ais-SearchBox-input" rows="6"></textarea>

      <button id="advanced-submit"
        class="wk-button wk-primary" type="submit">
        Search
      </button>

      <button class="wk-button" type="reset">
        Clear
      </button>
    </form>

    <div class="search-mode-options">
      <label class="search-mode-label"
        title="More Like This – similarity search">
        <input id="mlt-toggle" type="checkbox">
        MLT
      </label>

      <label class="search-mode-label"
        title="Advanced Lucene query syntax">
        <input id="lucene-toggle" type="checkbox">
        Lucene
      </label>
    </div>
  `;

  const mltToggle = document.getElementById("mlt-toggle");
  const luceneToggle = document.getElementById("lucene-toggle");
  const termBox = document.getElementById("term-searchbox");
  const form = document.getElementById("mlt-searchbox");
  const textarea = document.getElementById("mlt-text");
  const submit = document.getElementById("advanced-submit");

  const updateMode = () => {
    const advanced = useMlt || useLucene;

    mltToggle.checked = useMlt;
    luceneToggle.checked = useLucene;

    termBox.hidden = advanced;
    form.hidden = !advanced;

    textarea.placeholder = useLucene
      ? 'text:(maria AND virgo) AND meta.year:<1600'
      : "Paste a text to find similar OCR content...";

    textarea.setAttribute("aria-label", useLucene
      ? "Lucene query"
      : "Text for similarity search");

    submit.textContent = useLucene
      ? "Search Lucene query"
      : "Search similar text";
  };

  const switchMode = (mode, checked) => {
    const query = (useMlt || useLucene)
      ? textarea.value
      : search.helper.state.query || "";

    useMlt = mode === "mlt" && checked;
    useLucene = mode === "lucene" && checked;

    textarea.value = query;

    updateMode();

    search.helper
      .clearCache()
      .setQuery(query)
      .setPage(0)
      .search();

    if (useMlt || useLucene) {
      textarea.focus();
    } else {
      termBox.querySelector("input")?.focus();
    }
  };

  textarea.value = initialQuery;
  updateMode();

  mltToggle.addEventListener("change", () => {
    switchMode("mlt", mltToggle.checked);
  });

  luceneToggle.addEventListener("change", () => {
    switchMode("lucene", luceneToggle.checked);
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();

    search.helper
      .setQuery(textarea.value)
      .setPage(0)
      .search();
  });

  form.addEventListener("reset", (event) => {
    event.preventDefault();
    textarea.value = "";

    search.helper
      .setQuery("")
      .setPage(0)
      .search();

    textarea.focus();
  });
}

function initSearchMode_old(search, initialQuery) {
  document.getElementById("searchbox").innerHTML = `
    <label class="search-mode-label">
      <input id="mlt-toggle" type="checkbox"> More Like This (MLT)
    </label>
    <div id="term-searchbox"></div>
    <form id="mlt-searchbox" hidden>
      <textarea id="mlt-text" class="ais-SearchBox-input" rows="6"
        aria-label="Text for similarity search"
        placeholder="Paste a text to find similar OCR content..."></textarea>
      <button class="wk-button wk-primary" type="submit">Search similar text</button>
      <button class="wk-button" type="reset">Clear</button>
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

async function init(indexName = activeIndexName) {
  const version = ++initVersion;
  LibraryWorkspace.setCurrent([]);
  try {
    activeIndexName = indexName;
    const mapping = await loadMapping(indexName);
    if (version !== initVersion) return;
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

    const createSearchClient = (captureQueries = () => {}) => SearchkitInstantsearchClient(sk, {
      getQuery(query) {
        const q = (query || "").trim();

        if (!q) {
          return false;
        }

        if (useLucene) {
          return [{
            query_string: {
              query: q,
              default_field: "text",
              default_operator: "AND",
              lenient: false,
              allow_leading_wildcard: false
            }
          }];
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

        return queryDsl;
      },
      hooks: {
        beforeSearch: async (searchRequests) => {
          captureQueries(searchRequests);

          return searchRequests;
        }
      }
    });
    const requestTarget = `POST ${OPENSEARCH_HOST}/_msearch`;
    function recordBodies(requests, label) {
      const parts = requests.map(({ indexName, body }) => ({ indexName, body }));
      const entries = parts.map((part, i) => {
        const number = parts.length > 1 ? ` (${i + 1}/${parts.length})` : "";
        return LibraryWorkspace.record(`${label} · ${part.indexName}${number}`, requestTarget, part.body);
      });
      // Searchkit sends an index header and a query body per line pair, including the final newline.
      // Only build the copyable wire representation when someone selects it.
      entries.push(LibraryWorkspace.record(`${label} · full request (NDJSON)`, requestTarget, () =>
        parts.map(part => JSON.stringify({ index: part.indexName }) + "\n" + JSON.stringify(part.body) + "\n").join("")
      ));
      return entries;
    }

    // The facet-value client otherwise omits search options, including beforeSearch.
    const facetClient = SearchkitInstantsearchClient({
      handleInstantSearchRequests(requests) {
        return sk.handleInstantSearchRequests(requests, { hooks: {
          beforeSearch(batch) {
            recordBodies(batch, "Facet values");
            return batch;
          }
        } });
      }
    });
    const queryClients = new Map();
    let visibleQueryRequest = null;
    const searchClient = {
      search: (requests) => {
        const key = JSON.stringify({
          mode: useLucene ? "lucene" : useMlt ? "mlt" : "default",
          requests
        });
        let entry = queryClients.get(key);
        if (!entry) {
          entry = { queries: [] };
          // Keep each cached search paired with its final OpenSearch bodies.
          entry.client = createSearchClient((searchRequests) => {
            entry.queries = recordBodies(searchRequests, "OpenSearch");
            if (visibleQueryRequest === entry && version === initVersion) {
              LibraryWorkspace.setCurrent(entry.queries);
            }
          });
          queryClients.set(key, entry);
          if (queryClients.size > 50) queryClients.delete(queryClients.keys().next().value);
        }
        visibleQueryRequest = entry;
        LibraryWorkspace.setCurrent(entry.queries);
        return entry.client.search(requests);
      },
      searchForFacetValues: (requests) => facetClient.searchForFacetValues(requests),
      clearCache: () => {
        // Isolate late responses from searches started before a mode switch.
        queryClients.clear();
        visibleQueryRequest = null;
        LibraryWorkspace.setCurrent([]);
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
    const analysisLink = document.getElementById("analyze-terms");
    search.on("render", () => {
      const term = (search.helper?.state.query || "").trim();
      analysisLink.hidden = !term;
      if (term) {
        const url = new URL("../analyze/", document.baseURI);
        url.searchParams.set("q", term);
        analysisLink.href = url.href;
      }
    });
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
    if (version !== initVersion) return;
    console.error(err);
    document.getElementById("hits").innerHTML = `
      <div class="hit failure">
        <h2>Could not open this index</h2>
        <div class="snippet">${escapeHtml(err.message)}</div>
      </div>
    `;
  }
}

function initIndexSelector() {
  const select = document.getElementById("index-select");
  if (!select) return;

  if (IS_PRODUCTION) {
    select.closest(".index-field")?.remove();

    const panel = document.querySelector(".search-panel");
    const searchbox = document.getElementById("searchbox");

    panel.style.setProperty("display", "block", "important");
    searchbox.style.setProperty("width", "100%", "important");
    searchbox.style.setProperty("max-width", "none", "important");

    return;
  }

  select.value = activeIndexName;

  select.addEventListener("change", () => {
    const nextIndex = select.value;
    localStorage.setItem("ocrSearchIndex", nextIndex);
    init(nextIndex);
  });
}
initIndexSelector();
init(activeIndexName);
