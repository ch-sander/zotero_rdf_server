const OPENSEARCH_HOST = "/plugin/fts/search-proxy";
const INDEX_NAME = "ocr-scigma";

const SEARCH_ATTRIBUTES_OLD = [
  { field: "text", weight: 5 },
  { field: "label", weight: 3 },
  { field: "meta.file_label", weight: 3 },
  { field: "meta.parent_creators", weight: 2 },
  { field: "meta.parent_tags", weight: 2 },
  { field: "meta.library_label", weight: 2 },
  { field: "meta.file", weight: 1 }
];

const RESULT_ATTRIBUTES_OLD = [
  "doc_id",
  "label",
  "source",
  "page",
  "text",
  "ingest_ts",
  "meta.file",
  "meta.file_label",
  "meta.library",
  "meta.library_label",
  "meta.link_type",
  "meta.parent",
  "meta.parent_creators",
  "meta.parent_key",
  "meta.parent_tags",
  "meta.date"
];

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
  "meta.file"
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

async function loadMapping() {
  const res = await fetch(`${OPENSEARCH_HOST}/${INDEX_NAME}/_mapping`);
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
        const creators = hit.meta?.parent_creators || "-";
        const tags = hit.meta?.parent_tags || "-";
        const date = hit.meta?.date || "-";
        const snippet = components.Snippet({ attribute: "text", hit });
        const raw = hit.text ? hit.text.slice(0, 2000) : "";
        const url = buildUrl("doc", hit);

        return html`
          <div class="hit">
            <h2>${
                url
                  ? html`<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`
                  : label
              }</h2>

            <div class="meta">
              <span class="badge">ID: ${escapeHtml(hit.objectID)}</span>
              <span class="badge">Source: ${escapeHtml(source)}</span>
              <span class="badge">Page: ${escapeHtml(page)}</span>
              <span class="badge">Creators: ${escapeHtml(creators)}</span>
              <span class="badge">Date: ${escapeHtml(date)}</span>
              <span class="badge">Tags: ${escapeHtml(tags)}</span>
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

async function init() {
  try {
    const mapping = await loadMapping();
    const properties = mapping?.[INDEX_NAME]?.mappings?.properties || {};
    const flatFields = flattenProperties(properties);
    const dynamicFacets = buildFacetAttributes(flatFields);
    const dynamicResultAttributes = buildResultAttributes(flatFields);

    createFacetContainers(dynamicFacets);

    const sk = new Searchkit({
      connection: {
        host: OPENSEARCH_HOST
      },
      search_settings: {
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

    const searchClient = SearchkitInstantsearchClient(sk, {
      getQuery(query) {
        const q = (query || "").trim();
        if (!q) return false;

        const hasComma = query.includes(",");

        const parts = query
          .split(hasComma ? "," : " ")
          .map(s => s.trim())
          .filter(Boolean);

        if (hasComma) {
          return [
            {
              bool: {
                should: parts.flatMap((part) => [
                  {
                    match_phrase: {
                      text: {
                        query: part,
                        slop: 2,
                        boost: 4
                      }
                    }
                  },
                  {
                    match_phrase_prefix: {
                      text: {
                        query: part,
                        max_expansions: 50,
                        boost: 2
                      }
                    }
                  },
                  {
                    match: {
                      text: {
                        query: part,
                        fuzziness: 2,
                        prefix_length: 1,
                        max_expansions: 50,
                        boost: 1
                      }
                    }
                  }
                ]),
                minimum_should_match: 1
              }
            }
          ];
        }

        return [
          {
            bool: {
              must: parts.map((part) => ({
                bool: {
                  should: [
                    {
                      match_phrase: {
                        text: {
                          query: part,
                          slop: 2,
                          boost: 4
                        }
                      }
                    },
                    {
                      match_phrase_prefix: {
                        text: {
                          query: part,
                          max_expansions: 50,
                          boost: 2
                        }
                      }
                    },
                    {
                      match: {
                        text: {
                          query: part,
                          fuzziness: 2,
                          prefix_length: 1,
                          max_expansions: 50,
                          boost: 1
                        }
                      }
                    }
                  ],
                  minimum_should_match: 1
                }
              }))
            }
          }
        ];

      }
    });

    const search = instantsearch({
      indexName: INDEX_NAME,
      searchClient
    });

    search.addWidgets([
      instantsearch.widgets.searchBox({
        container: "#searchbox",
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

init();

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