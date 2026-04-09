(function () {
  "use strict";

  const CONFIG = window.ANALYSIS_SEARCH_EXPLORER_CONFIG || {};
  const UI_TEXT = CONFIG.ui || {};

  const EXPLORER_CONFIG = {
    endpointPath: CONFIG.endpointPath || "/plugin/fts/search/terms",
    openapiUrl: CONFIG.openapiUrl || "/openapi.json",
    apiBaseUrl: CONFIG.apiBaseUrl || "",
    initialHits: Array.isArray(CONFIG.initialHits) ? CONFIG.initialHits : [],
    maxTermBadgesPerSection: Number(CONFIG.maxTermBadgesPerSection || 20),
    maxSourceFieldsPreview: Number(CONFIG.maxSourceFieldsPreview || 12),
    includeGlobalTerms: CONFIG.includeGlobalTerms !== false,
    includeLocalTerms: CONFIG.includeLocalTerms !== false,
    includeClusterInfo: CONFIG.includeClusterInfo !== false,
    pageSize: Number(CONFIG.pageSize || 25),
    preferredTitleFields: Array.isArray(CONFIG.preferredTitleFields)
      ? CONFIG.preferredTitleFields
      : ["title", "name", "label", "headline", "subject", "_id"],
    preferredSnippetFields: Array.isArray(CONFIG.preferredSnippetFields)
      ? CONFIG.preferredSnippetFields
      : ["snippet", "summary", "description", "content", "text", "body"],
    links: {
      doc: {
        template: CONFIG.docLinkTemplate || "/plugin/fts/view/{os_doc_id}",
        map: (item) => ({
          os_doc_id: item.id
        })
      }
    }
  };

  let ENDPOINT_PARAMETERS = [];
  let allPreparedHits = [];
  let activeTerm = "";
  let rawResponseVisible = false;
  let currentPage = 1;
  let pageSize = EXPLORER_CONFIG.pageSize;

  const contentEl = document.getElementById("content");
  const resultInfoEl = document.getElementById("resultInfo");
  const sidebarClustersEl = document.getElementById("sidebarClusters");
  const sidebarClusterInfoEl = document.getElementById("sidebarClusterInfo");
  const topClusterTermsEl = document.getElementById("topClusterTerms");

  const clientSearchInput = document.getElementById("clientSearchInput");
  const clientClusterSelect = document.getElementById("clientClusterSelect");
  const resetClientFiltersBtn = document.getElementById("resetClientFiltersBtn");
  const expandAllBtn = document.getElementById("expandAllBtn");

  const apiSearchForm = document.getElementById("apiSearchForm");
  const dynamicFormSections = document.getElementById("dynamicFormSections");
  const submitApiSearchBtn = document.getElementById("submitApiSearchBtn");
  const atlasSearchBtn = document.getElementById("atlasSearchBtn");
  const resetApiFormBtn = document.getElementById("resetApiFormBtn");
  const apiStatusEl = document.getElementById("apiStatus");
  const requestUrlBoxEl = document.getElementById("requestUrlBox");
  const requestUrlPreEl = document.getElementById("requestUrlPre");
  const toggleRawResponseBtn = document.getElementById("toggleRawResponseBtn");
  const rawResponseEl = document.getElementById("rawResponse");

  const summaryDocumentsEl = document.getElementById("summaryDocuments");
  const summaryClustersEl = document.getElementById("summaryClusters");
  const summaryUnclusteredEl = document.getElementById("summaryUnclustered");
  const summaryClusterTermsEl = document.getElementById("summaryClusterTerms");

  function buildUrl(type, item) {
    const cfg = EXPLORER_CONFIG.links?.[type];
    if (!cfg) return "";

    const vars = cfg.map ? cfg.map(item) : item;

    const result = cfg.template.replace(/\{(\w+)\}/g, (_, key) =>
      encodeURIComponent(vars[key] ?? "")
    );
    return result;
  }

  function initStaticTexts() {
    document.documentElement.lang = UI_TEXT.lang || "en";
    document.title = CONFIG.title || "Analysis Search Explorer";

    const sidebarTitle = document.getElementById("sidebarTitle");
    const pageTitle = document.getElementById("pageTitle");
    const pageSubtitle = document.getElementById("pageSubtitle");

    if (sidebarTitle) sidebarTitle.textContent = CONFIG.title || "Analysis Search Explorer";
    if (pageTitle) pageTitle.textContent = CONFIG.title || "Analysis Search Explorer";
    if (pageSubtitle) pageSubtitle.textContent = CONFIG.subtitle || "Interactive search, clustering, and analysis explorer.";

    if (clientSearchInput) {
      clientSearchInput.placeholder = UI_TEXT.search_placeholder || "Search title, snippet, ID, terms ...";
    }
    if (submitApiSearchBtn) submitApiSearchBtn.textContent = UI_TEXT.search || "Search";
    if (atlasSearchBtn) atlasSearchBtn.textContent = UI_TEXT.atlas || "Atlas";
    if (resetApiFormBtn) resetApiFormBtn.textContent = UI_TEXT.reset_form || "Reset form";
    if (resetClientFiltersBtn) resetClientFiltersBtn.textContent = UI_TEXT.reset_filters || "Reset filters";
    if (expandAllBtn) expandAllBtn.textContent = UI_TEXT.toggle_all_details || "Toggle all details";
    if (toggleRawResponseBtn) toggleRawResponseBtn.textContent = UI_TEXT.show_raw_response || "Show raw response";
  }

  function normalize(value) {
    return (value || "").toString().toLowerCase().trim();
  }

  function safeStr(value) {
    if (value === null || value === undefined) return "";
    return String(value);
  }

  function escapeHtml(value) {
    return safeStr(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtScore(value) {
    const n = Number(value || 0);
    return Number.isFinite(n) ? n.toFixed(4) : "0.0000";
  }

  function jsonLike(value, depth = 0) {
    if (depth > 2) return "...";

    if (Array.isArray(value)) {
      const parts = value.slice(0, 10).map(v => jsonLike(v, depth + 1));
      if (value.length > 10) parts.push("...");
      return "[ " + parts.join(", ") + " ]";
    }

    if (value && typeof value === "object") {
      const entries = Object.entries(value)
        .slice(0, 20)
        .map(([k, v]) => `${k}: ${jsonLike(v, depth + 1)}`);
      if (Object.keys(value).length > 20) entries.push("...");
      return "{ " + entries.join(", ") + " }";
    }

    return safeStr(value);
  }

  function getSource(hit) {
    if (!hit || typeof hit !== "object") return {};
    if (hit._source && typeof hit._source === "object") return hit._source;
    return hit;
  }

  function getAnalysis(hit) {
    const source = getSource(hit);
    return source.analysis && typeof source.analysis === "object" ? source.analysis : {};
  }

  function getCluster(hit) {
    const analysis = getAnalysis(hit);
    return analysis.cluster && typeof analysis.cluster === "object" ? analysis.cluster : {};
  }

  function getLocal(hit) {
    const analysis = getAnalysis(hit);
    return analysis.local && typeof analysis.local === "object" ? analysis.local : {};
  }

  function getGlobal(hit) {
    const analysis = getAnalysis(hit);
    return analysis.global && typeof analysis.global === "object" ? analysis.global : {};
  }

  function pickTitle(hit) {
    const src = getSource(hit);
    for (const field of EXPLORER_CONFIG.preferredTitleFields) {
      if (field === "_id") break;
      const value = src[field];
      if (value !== null && value !== undefined && value !== "" && (!Array.isArray(value) || value.length > 0)) {
        if (Array.isArray(value)) return value.slice(0, 3).map(safeStr).join(" | ");
        return safeStr(value);
      }
    }
    return safeStr(hit && hit._id ? hit._id : "untitled");
  }

  function normalizeSnippetText(text) {
    return safeStr(text)
      .replace(/ſ/g, "s")
      .replace(/æ/g, "ae")
      .replace(/œ/g, "oe")
      .replace(/\s*¬\s*/g, "")
      .replace(/-\s*\r?\n\s*/g, "")
      .replace(/\s*\r?\n+\s*/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function pickSnippet(hit) {
    const highlight = hit && hit.highlight && typeof hit.highlight === "object" ? hit.highlight : null;

    if (highlight) {
      const firstKey = Object.keys(highlight)[0];
      if (firstKey && Array.isArray(highlight[firstKey]) && highlight[firstKey].length) {
        return {
          fragments: highlight[firstKey].map(t => normalizeSnippetText(t)),
          isHighlighted: true
        };
      }
    }

    const src = getSource(hit);
    for (const field of EXPLORER_CONFIG.preferredSnippetFields) {
      const value = src[field];
      if (value) {
        const text = Array.isArray(value)
          ? value.slice(0, 5).map(safeStr).join(" ")
          : safeStr(value);

        return {
          fragments: [normalizeSnippetText(text)],
          isHighlighted: false
        };
      }
    }

    return {
      fragments: [],
      isHighlighted: false
    };
  }

  function extractKeyTerms(section) {
    const terms = section && Array.isArray(section.key_terms) ? section.key_terms : [];
    return terms.map(safeStr).map(s => s.trim()).filter(Boolean);
  }

  function extractKeyTermDetails(section) {
    const details = section && Array.isArray(section.key_terms_details) ? section.key_terms_details : [];
    return details.filter(item => item && typeof item === "object" && item.term);
  }

  function extractClusterLabelTerms(cluster) {
    const terms = cluster && Array.isArray(cluster.label_terms) ? cluster.label_terms : [];
    return terms.map(safeStr).map(s => s.trim()).filter(Boolean);
  }

  function uniqueTerms(items) {
    const out = [];
    const seen = new Set();

    for (const item of items) {
      const s = safeStr(item).trim();
      if (!s || seen.has(s)) continue;
      seen.add(s);
      out.push(s);
    }

    return out;
  }

  function renderTermBadges(terms, cssClass = "term-badge", limit = EXPLORER_CONFIG.maxTermBadgesPerSection) {
    const unique = uniqueTerms(terms).slice(0, limit);
    if (!unique.length) return `<span class="muted">–</span>`;

    return unique.map(term =>
      `<button class="${cssClass}" data-term="${escapeHtml(term)}" type="button">${escapeHtml(term)}</button>`
    ).join("");
  }

  function renderTermDetails(details) {
    if (!details.length) {
      return `<div class="muted">${escapeHtml(UI_TEXT.no_details || "No details")}</div>`;
    }

    const rows = details.slice(0, EXPLORER_CONFIG.maxTermBadgesPerSection).map(item => (
      "<tr>" +
        `<td>${escapeHtml(item.term)}</td>` +
        `<td>${escapeHtml(fmtScore(item.score))}</td>` +
        `<td>${escapeHtml(safeStr(item.doc_freq ?? ""))}</td>` +
        `<td>${escapeHtml(safeStr(item.term_freq ?? ""))}</td>` +
      "</tr>"
    )).join("");

    return (
      `<table class="mini-table">` +
      `<thead><tr>` +
      `<th>${escapeHtml(UI_TEXT.term || "term")}</th>` +
      `<th>${escapeHtml(UI_TEXT.score || "score")}</th>` +
      `<th>${escapeHtml(UI_TEXT.doc_freq || "doc_freq")}</th>` +
      `<th>${escapeHtml(UI_TEXT.term_freq || "term_freq")}</th>` +
      `</tr></thead>` +
      `<tbody>${rows}</tbody>` +
      `</table>`
    );
  }

  function renderSourcePreview(src) {
    const keys = Object.keys(src || {}).filter(k => k !== "analysis");
    if (!keys.length) {
      return `<div class="muted">${escapeHtml(UI_TEXT.no_fields || "No fields")}</div>`;
    }

    const rows = keys.slice(0, EXPLORER_CONFIG.maxSourceFieldsPreview).map(key => (
      "<tr>" +
        `<td>${escapeHtml(key)}</td>` +
        `<td>${escapeHtml(jsonLike(src[key]))}</td>` +
      "</tr>"
    )).join("");

    return (
      `<table class="mini-table">` +
      `<thead><tr><th>${escapeHtml(UI_TEXT.field || "field")}</th><th>${escapeHtml(UI_TEXT.value || "value")}</th></tr></thead>` +
      `<tbody>${rows}</tbody>` +
      `</table>`
    );
  }

  function normalizeHitsFromResponse(data) {
    if (!data) return [];
    if (Array.isArray(data)) return data;
    if (Array.isArray(data.hits)) return data.hits;
    if (data.hits && Array.isArray(data.hits.hits)) return data.hits.hits;
    if (Array.isArray(data.items)) return data.items;
    return [];
  }

  function stripHtmlTags(text) {
    const div = document.createElement("div");
    div.innerHTML = safeStr(text);
    return (div.textContent || div.innerText || "").trim();
  }

  function snippetToSearchText(snippet) {
    if (!snippet || !Array.isArray(snippet.fragments)) return "";
    return snippet.fragments
      .map(fragment => stripHtmlTags(fragment))
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function isPrimitiveFacetValue(value) {
    return value === null || value === undefined ||
      typeof value === "string" ||
      typeof value === "number" ||
      typeof value === "boolean";
  }

  function normalizeFacetValue(value) {
    if (value === null || value === undefined) return "";
    return String(value).trim();
  }

  function extractMetaFacets(meta, allowedKeys) {
    const out = {};

    function isAllowed(relativePath) {
      if (!allowedKeys || !allowedKeys.length) return true;
      return allowedKeys.includes(relativePath);
    }

    function addValue(absPath, value) {
      const v = normalizeFacetValue(value);
      if (!v) return;
      if (!out[absPath]) out[absPath] = [];
      if (!out[absPath].includes(v)) out[absPath].push(v);
    }

    function walk(value, relativePath, absolutePath) {
      if (Array.isArray(value)) {
        if (!isAllowed(relativePath)) return;

        for (const item of value) {
          if (isPrimitiveFacetValue(item)) {
            addValue(absolutePath, item);
          } else if (item && typeof item === "object") {
            walk(item, relativePath, absolutePath);
          }
        }
        return;
      }

      if (isPrimitiveFacetValue(value)) {
        if (!isAllowed(relativePath)) return;
        addValue(absolutePath, value);
        return;
      }

      if (value && typeof value === "object") {
        for (const [key, child] of Object.entries(value)) {
          const nextRelative = relativePath ? `${relativePath}.${key}` : key;
          const nextAbsolute = absolutePath ? `${absolutePath}.${key}` : key;
          walk(child, nextRelative, nextAbsolute);
        }
      }
    }

    walk(meta || {}, "", "meta");
    return out;
  }

  function prepareHits(hits) {
    return (hits || []).map((hit, idx) => {
      const cluster = getCluster(hit);
      const local = getLocal(hit);
      const global = getGlobal(hit);
      const source = getSource(hit);

      const hitId = safeStr(hit && hit._id ? hit._id : `hit-${idx}`);
      const score = hit && hit._score !== undefined ? hit._score : 0;
      const label = safeStr(source.label || "");
      const page = safeStr(source.page ?? hit.page ?? "");
      const title = pickTitle(hit);
      const snippet = pickSnippet(hit);

      const clusterId = cluster.id === null || cluster.id === undefined ? "unclustered" : safeStr(cluster.id);
      const clusterLabel = safeStr(cluster.label || UI_TEXT.unclustered || "Unclustered") || (UI_TEXT.unclustered || "Unclustered");
      const clusterLabelTerms = extractClusterLabelTerms(cluster);
      const localTerms = extractKeyTerms(local);
      const globalTerms = extractKeyTerms(global);
      const searchableTerms = uniqueTerms([...clusterLabelTerms, ...localTerms, ...globalTerms]);
      const snippetSearchText = snippetToSearchText(snippet);
      const metaFacetConfig = (window.ANALYSIS_SEARCH_EXPLORER_CONFIG && window.ANALYSIS_SEARCH_EXPLORER_CONFIG.metaFacets) || {};
      const allowedMetaFacetKeys = Array.isArray(metaFacetConfig.keys)
        ? metaFacetConfig.keys.map(s => safeStr(s).trim()).filter(Boolean)
        : [];
      const metaFacets = extractMetaFacets(source.meta || {}, allowedMetaFacetKeys);

      return {
        raw: hit,
        idx,
        id: hitId,
        label,
        score,
        page,
        title,
        snippet,
        source,
        cluster,
        local,
        global,
        metaFacets,
        clusterId,
        clusterLabel,
        clusterLabelTerms,
        localTerms,
        globalTerms,
        searchBlob: `${hitId} ${title} ${snippetSearchText} ${clusterLabel} ${searchableTerms.join(" ")}`
          .toLowerCase()
          .replace(/\s+/g, " ")
          .trim()
      };
    });
  }

  function groupPreparedHits(preparedHits) {
    const clusters = new Map();

    for (const item of preparedHits) {
      if (!clusters.has(item.clusterId)) {
        clusters.set(item.clusterId, {
          id: item.clusterId,
          label: item.clusterLabel,
          labelTerms: item.clusterLabelTerms.slice(),
          docs: []
        });
      }
      clusters.get(item.clusterId).docs.push(item);
    }

    for (const cluster of clusters.values()) {
      cluster.docs.sort((a, b) => Number(b.score || 0) - Number(a.score || 0));
    }

    return Array.from(clusters.values()).sort((a, b) => {
      const aUn = a.id === "unclustered" ? 1 : 0;
      const bUn = b.id === "unclustered" ? 1 : 0;
      if (aUn !== bUn) return aUn - bUn;

      const aTop = a.docs.length ? Number(a.docs[0].score || 0) : 0;
      const bTop = b.docs.length ? Number(b.docs[0].score || 0) : 0;
      if (aTop !== bTop) return bTop - aTop;

      return safeStr(a.id).localeCompare(safeStr(b.id));
    });
  }

  function filterPreparedHits(preparedHits) {
    const search = normalize(clientSearchInput.value);
    const selectedCluster = normalize(clientClusterSelect.value);

    const metaFacetState =
      window.ANALYSIS_SEARCH_EXPLORER_META_STATE &&
      typeof window.ANALYSIS_SEARCH_EXPLORER_META_STATE.matchesPreparedHit === "function"
        ? window.ANALYSIS_SEARCH_EXPLORER_META_STATE
        : null;

    return preparedHits.filter(item => {
      const clusterOk = !selectedCluster || normalize(item.clusterId) === selectedCluster;
      const searchOk = !search || item.searchBlob.includes(search);

      const termSpace = normalize([
        ...item.clusterLabelTerms,
        ...item.localTerms,
        ...item.globalTerms
      ].join(" "));

      const termOk = !activeTerm || termSpace.includes(activeTerm);
      const metaOk = !metaFacetState || metaFacetState.matchesPreparedHit(item);

      return clusterOk && searchOk && termOk && metaOk;
    });
  }

  function paginateItems(items, page, size) {
    const total = items.length;
    const totalPages = Math.max(1, Math.ceil(total / size));
    const safePage = Math.min(Math.max(1, page), totalPages);
    const start = (safePage - 1) * size;
    const end = start + size;

    return {
      pageItems: items.slice(start, end),
      page: safePage,
      pageSize: size,
      totalItems: total,
      totalPages,
      startIndex: total ? start + 1 : 0,
      endIndex: Math.min(end, total)
    };
  }

  function rebuildClientClusterSelect(filteredFromSearchOnlyHits) {
    const currentValue = clientClusterSelect.value || "";
    const grouped = groupPreparedHits(filteredFromSearchOnlyHits);
    const options = [`<option value="">${escapeHtml(UI_TEXT.all_clusters || "All clusters")}</option>`];

    for (const cluster of grouped) {
      options.push(
        `<option value="${escapeHtml(cluster.id)}">${escapeHtml(cluster.label)} (${cluster.docs.length})</option>`
      );
    }

    clientClusterSelect.innerHTML = options.join("");
    const stillExists = Array.from(clientClusterSelect.options).some(opt => opt.value === currentValue);
    clientClusterSelect.value = stillExists ? currentValue : "";
  }

  function renderSidebar(clusters, preparedHits) {
    const unclusteredCount = preparedHits.filter(h => h.clusterId === "unclustered").length;
    const visibleClusterCount = clusters.filter(c => c.id !== "unclustered").length;

    sidebarClusterInfoEl.textContent =
      `${preparedHits.length} ${(UI_TEXT.documents || "Documents").toLowerCase()}, ${visibleClusterCount} ${(UI_TEXT.clusters || "Clusters").toLowerCase()}, ${unclusteredCount} ${(UI_TEXT.unclustered || "Unclustered").toLowerCase()}`;

    const allClusterTerms = [];
    for (const cluster of clusters) {
      for (const term of cluster.labelTerms || []) allClusterTerms.push(term);
    }

    const counts = new Map();
    for (const term of allClusterTerms) {
      counts.set(term, (counts.get(term) || 0) + 1);
    }

    const topTerms = Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 40)
      .map(([term]) => term);

    topClusterTermsEl.innerHTML = renderTermBadges(topTerms, "term-badge top-term", 40);

    if (!clusters.length) {
      sidebarClustersEl.innerHTML = `<div class="muted">${escapeHtml(UI_TEXT.no_clusters || "No clusters")}</div>`;
      return;
    }

    sidebarClustersEl.innerHTML = clusters.map(cluster => (
      `<a class="sidebar-cluster-link" href="#cluster-${escapeHtml(cluster.id)}" data-cluster="${escapeHtml(cluster.id)}">` +
        `<span class="sidebar-cluster-title">${escapeHtml(cluster.label)}</span>` +
        `<span class="sidebar-cluster-meta">${cluster.docs.length} ${escapeHtml(UI_TEXT.docs || "docs")}</span>` +
      `</a>`
    )).join("");
  }

  function renderSummary(clusters, preparedHits) {
    const unclusteredCount = preparedHits.filter(h => h.clusterId === "unclustered").length;
    const visibleClusterCount = clusters.filter(c => c.id !== "unclustered").length;

    const clusterTerms = new Set();
    for (const cluster of clusters) {
      for (const term of cluster.labelTerms || []) clusterTerms.add(term);
    }

    summaryDocumentsEl.textContent = String(preparedHits.length);
    summaryClustersEl.textContent = String(visibleClusterCount);
    summaryUnclusteredEl.textContent = String(unclusteredCount);
    summaryClusterTermsEl.textContent = String(clusterTerms.size);
  }

  function renderPagination(pager) {
    return (
      `<div class="pagination-bar">` +
        `<div class="pagination-info">` +
          `${escapeHtml(UI_TEXT.showing_results || "Showing results")}: ${pager.startIndex}-${pager.endIndex} / ${pager.totalItems}` +
        `</div>` +
        `<div class="pagination-spacer"></div>` +
        `<label class="pagination-info" for="pageSizeSelect">${escapeHtml(UI_TEXT.page_size || "Page size")}</label>` +
        `<select id="pageSizeSelect" class="select" style="width:auto;">` +
          [10, 25, 50, 100, 200, 500].map(size =>
            `<option value="${size}"${size === pager.pageSize ? " selected" : ""}>${size}</option>`
          ).join("") +
        `</select>` +
        `<button id="prevPageBtn" class="btn btn-secondary" type="button"${pager.page <= 1 ? " disabled" : ""}>${escapeHtml(UI_TEXT.prev || "Previous")}</button>` +
        `<div class="page-indicator">${escapeHtml(UI_TEXT.page || "Page")} ${pager.page} ${escapeHtml(UI_TEXT.of || "of")} ${pager.totalPages}</div>` +
        `<button id="nextPageBtn" class="btn btn-secondary" type="button"${pager.page >= pager.totalPages ? " disabled" : ""}>${escapeHtml(UI_TEXT.next || "Next")}</button>` +
      `</div>`
    );
  }

  function sanitizeHighlightHtml(html) {
    const template = document.createElement("template");
    template.innerHTML = safeStr(html);

    const allowedTags = new Set(["EM", "STRONG", "MARK"]);

    function clean(node) {
      const children = Array.from(node.childNodes);

      for (const child of children) {
        if (child.nodeType === Node.ELEMENT_NODE) {
          if (!allowedTags.has(child.tagName)) {
            const textNode = document.createTextNode(child.textContent || "");
            child.replaceWith(textNode);
            continue;
          }

          const attrs = Array.from(child.attributes);
          for (const attr of attrs) {
            child.removeAttribute(attr.name);
          }

          clean(child);
        } else if (child.nodeType !== Node.TEXT_NODE) {
          child.remove();
        }
      }
    }

    clean(template.content);

    return template.innerHTML
      .replace(/<em>/gi, '<mark class="search-hit">')
      .replace(/<\/em>/gi, "</mark>")
      .replace(/<strong>/gi, '<mark class="search-hit strong-hit">')
      .replace(/<\/strong>/gi, "</mark>");
  }

  function renderSnippet(snippet) {
    if (!snippet || !snippet.fragments || !snippet.fragments.length) {
      return `<span class="muted">${escapeHtml(UI_TEXT.no_snippet || "No snippet")}</span>`;
    }

    const items = snippet.fragments.map(fragment => {
      if (snippet.isHighlighted) {
        return `<li>${sanitizeHighlightHtml(fragment)}</li>`;
      }
      return `<li>${escapeHtml(fragment)}</li>`;
    }).join("");

    return `<ul class="snippet-list">${items}</ul>`;
  }

  function renderContentFromClusters(clusters, pager) {
    if (!clusters.length) {
      contentEl.innerHTML = (
        `<div class="muted">${escapeHtml(UI_TEXT.no_content || "No content")}</div>` +
        renderPagination(pager)
      );
      wirePaginationControls(pager);
      return;
    }

    const sectionsHtml = clusters.map(cluster => {
      const docsHtml = cluster.docs.map(item => {
        const snippetHtml = renderSnippet(item.snippet);

        const clusterInfoHtml = EXPLORER_CONFIG.includeClusterInfo ? (
          `<section class="details-section">` +
            `<h4>${escapeHtml(UI_TEXT.cluster_info || "Cluster")}</h4>` +
            `<div class="kv-grid">` +
              `<div><strong>id</strong><div>${escapeHtml(safeStr(item.cluster.id ?? ""))}</div></div>` +
              `<div><strong>label</strong><div>${escapeHtml(safeStr(item.cluster.label ?? ""))}</div></div>` +
              `<div><strong>${escapeHtml(UI_TEXT.size || "size")}</strong><div>${escapeHtml(safeStr(item.cluster.size ?? ""))}</div></div>` +
              `<div><strong>${escapeHtml(UI_TEXT.source || "source")}</strong><div>${escapeHtml(safeStr(item.cluster.source ?? ""))}</div></div>` +
              `<div><strong>${escapeHtml(UI_TEXT.label_source || "label_source")}</strong><div>${escapeHtml(safeStr(item.cluster.label_source ?? ""))}</div></div>` +
            `</div>` +
          `</section>`
        ) : "";

        const localHtml = EXPLORER_CONFIG.includeLocalTerms ? (
          `<div class="doc-chip-row">` +
            `<span class="chip-label">${escapeHtml(UI_TEXT.local_terms || "Local terms")}</span>` +
            renderTermBadges(item.localTerms, "term-badge local-term") +
          `</div>`
        ) : "";

        const localDetailsHtml = EXPLORER_CONFIG.includeLocalTerms ? (
          `<section class="details-section">` +
            `<h4>${escapeHtml(UI_TEXT.local_analysis || "Local analysis")}</h4>` +
            renderTermDetails(extractKeyTermDetails(item.local)) +
          `</section>`
        ) : "";

        const globalHtml = EXPLORER_CONFIG.includeGlobalTerms ? (
          `<div class="doc-chip-row">` +
            `<span class="chip-label">${escapeHtml(UI_TEXT.global_terms || "Global terms")}</span>` +
            renderTermBadges(item.globalTerms, "term-badge global-term") +
          `</div>`
        ) : "";

        const globalDetailsHtml = EXPLORER_CONFIG.includeGlobalTerms ? (
          `<section class="details-section">` +
            `<h4>${escapeHtml(UI_TEXT.global_analysis || "Global analysis")}</h4>` +
            renderTermDetails(extractKeyTermDetails(item.global)) +
          `</section>`
        ) : "";

        const url = buildUrl("doc", item);

        const titleHtml = url
          ? `<a href="${escapeHtml(url)}" target="_blank">${escapeHtml(item.title)}</a>`
          : escapeHtml(item.title);

        const metaLabelHtml = item.label
          ? (url
              ? `<a href="${escapeHtml(url)}" target="_blank">${escapeHtml(item.label)}</a>`
              : escapeHtml(item.label))
          : "";

        return (
          `<article class="doc-card" id="doc-${escapeHtml(item.id)}">` +
            `<div class="doc-card-header">` +
              `<div class="doc-card-title">${titleHtml}</div>` +
              `<div class="doc-card-meta">` +
                `<span><strong>${escapeHtml(UI_TEXT.id || "ID")}:</strong> ${escapeHtml(item.id)}</span>` +
                `<span><strong>${escapeHtml(UI_TEXT.page || "Page")}:</strong> ${escapeHtml(safeStr(item.page ?? ""))}</span>` +
                (metaLabelHtml
                  ? `<span><strong>${escapeHtml(UI_TEXT.label || "Label")}:</strong> ${metaLabelHtml}</span>`
                  : "") +
                `<span><strong>${escapeHtml(UI_TEXT.score || "score")}:</strong> ${escapeHtml(fmtScore(item.score))}</span>` +
                `<span><strong>${escapeHtml(UI_TEXT.cluster || "Cluster")}:</strong> ${escapeHtml(item.clusterLabel)}</span>` +
              `</div>` +
            `</div>` +

            `<div class="doc-snippet">${snippetHtml}</div>` +

            `<div class="doc-chip-row">` +
              `<span class="chip-label">${escapeHtml(UI_TEXT.cluster_terms || "Cluster terms")}</span>` +
              renderTermBadges(item.clusterLabelTerms, "term-badge cluster-term") +
            `</div>` +

            localHtml +
            globalHtml +

            `<details class="details-block">` +
              `<summary>${escapeHtml(UI_TEXT.analysis_details || "Analysis details")}</summary>` +
              clusterInfoHtml +
              localDetailsHtml +
              globalDetailsHtml +
              `<section class="details-section">` +
                `<h4>${escapeHtml(UI_TEXT.source_preview || "Source preview")}</h4>` +
                renderSourcePreview(item.source) +
              `</section>` +
            `</details>` +
          `</article>`
        );
      }).join("");

      return (
        `<section class="cluster-section" id="cluster-${escapeHtml(cluster.id)}" data-cluster="${escapeHtml(cluster.id)}">` +
          `<div class="cluster-header">` +
            `<h2>${escapeHtml(cluster.label)}</h2>` +
            `<div class="cluster-meta">${cluster.docs.length} ${escapeHtml((UI_TEXT.documents || "Documents").toLowerCase())}</div>` +
          `</div>` +
          `<div class="cluster-terms-wrap">${renderTermBadges(cluster.labelTerms, "term-badge cluster-term")}</div>` +
          `<div class="cluster-docs">${docsHtml}</div>` +
        `</section>`
      );
    }).join("");

    contentEl.innerHTML = sectionsHtml + renderPagination(pager);
    wirePaginationControls(pager);
  }

  function wirePaginationControls(pager) {
    const prevBtn = document.getElementById("prevPageBtn");
    const nextBtn = document.getElementById("nextPageBtn");
    const pageSizeSelect = document.getElementById("pageSizeSelect");

    if (prevBtn) {
      prevBtn.addEventListener("click", function () {
        if (pager.page > 1) {
          currentPage = pager.page - 1;
          refreshView();
          window.scrollTo({ top: 0, behavior: "smooth" });
        }
      });
    }

    if (nextBtn) {
      nextBtn.addEventListener("click", function () {
        if (pager.page < pager.totalPages) {
          currentPage = pager.page + 1;
          refreshView();
          window.scrollTo({ top: 0, behavior: "smooth" });
        }
      });
    }

    if (pageSizeSelect) {
      pageSizeSelect.addEventListener("change", function () {
        const nextSize = Number(pageSizeSelect.value || EXPLORER_CONFIG.pageSize || 25);
        pageSize = Number.isFinite(nextSize) && nextSize > 0 ? nextSize : 25;
        currentPage = 1;
        refreshView();
      });
    }
  }

  function syncActiveTermUI() {
    const buttons = Array.from(document.querySelectorAll(".term-badge[data-term]"));
    buttons.forEach(btn => {
      const term = normalize(btn.dataset.term || "");
      btn.classList.toggle("active-term", !!activeTerm && term === activeTerm);
    });
  }

  function refreshView() {
    const searchOnlyHits = allPreparedHits.filter(item => {
      const search = normalize(clientSearchInput.value);
      if (!search) return true;
      return item.searchBlob.includes(search);
    });

    rebuildClientClusterSelect(searchOnlyHits);

    const baseFilteredHits = filterPreparedHits(allPreparedHits);

    const externalFacetFilter =
      typeof window.ANALYSIS_SEARCH_EXPLORER_META_FACET_FILTER === "function"
        ? window.ANALYSIS_SEARCH_EXPLORER_META_FACET_FILTER
        : null;

    const filteredHits = externalFacetFilter
      ? baseFilteredHits.filter(item => externalFacetFilter(item.raw))
      : baseFilteredHits;

    const filteredClusters = groupPreparedHits(filteredHits);

    renderSidebar(filteredClusters, filteredHits);
    renderSummary(filteredClusters, filteredHits);

    const pager = paginateItems(filteredHits, currentPage, pageSize);
    currentPage = pager.page;

    const pageClusters = groupPreparedHits(pager.pageItems);
    renderContentFromClusters(pageClusters, pager);

    const visibleClusters = filteredClusters.filter(c => c.id !== "unclustered").length;
    let info = `${filteredHits.length} ${UI_TEXT.documents_visible || "documents visible"}, ${visibleClusters} ${UI_TEXT.clusters_visible || "clusters visible"}`;
    if (activeTerm) info += `, ${UI_TEXT.active_term || "active term"}: "${activeTerm}"`;
    resultInfoEl.textContent = info;

    syncActiveTermUI();
  }

  function setActiveTerm(term) {
    activeTerm = normalize(term);
    currentPage = 1;
    refreshView();
  }

  function setApiStatus(text, kind = "") {
    apiStatusEl.textContent = text;
    apiStatusEl.className = "status-line" + (kind ? ` ${kind}` : "");
  }

  function setRawResponse(data) {
    try {
      rawResponseEl.textContent = JSON.stringify(data, null, 2);
    } catch (_err) {
      rawResponseEl.textContent = safeStr(data);
    }
  }

  function schema(param) {
    return param && typeof param === "object" && param.schema && typeof param.schema === "object"
      ? param.schema
      : {};
  }

  function schemaDefault(param, fallback = "") {
    const s = schema(param);
    return Object.prototype.hasOwnProperty.call(s, "default") ? s.default : fallback;
  }

  function schemaType(param) {
    const s = schema(param);
    if (Array.isArray(s.enum)) return "enum";
    if (Array.isArray(s.anyOf)) {
      for (const item of s.anyOf) {
        if (item && item.type && item.type !== "null") {
          return String(item.type);
        }
      }
      return "string";
    }
    return String(s.type || "string");
  }

  function schemaEnum(param) {
    const s = schema(param);
    if (Array.isArray(s.enum)) {
      return s.enum.map(v => String(v));
    }
    return null;
  }

  function schemaDescription(param) {
    return safeStr(param.description || schema(param).description || "");
  }

  function renderLabel(text, required = false) {
    const req = required
      ? ` <span class="required-pill">${escapeHtml(UI_TEXT.required || "required")}</span>`
      : "";
    return `${escapeHtml(text)}${req}`;
  }

  function groupParameter(name) {
    if (
      name === "q" ||
      ["index", "field", "size", "offset", "exact", "truncated", "fuzzy", "lucene", "highlight"].includes(name)
    ) {
      return "search";
    }
    if (
      name === "perform_analysis" ||
      name.startsWith("analyze_") ||
      name.startsWith("cluster_") ||
      name === "analysis_mode"
    ) {
      return "analysis";
    }
    return "advanced";
  }

  function renderParamControl(param) {
    const name = safeStr(param.name);
    if (!name) return "";

    const label = name.replace(/_/g, " ");
    const description = schemaDescription(param);
    const required = !!param.required;
    const typ = schemaType(param);
    const enumValues = schemaEnum(param);
    const defaultValue = schemaDefault(param, "");
    const valueAttr = escapeHtml(safeStr(defaultValue));
    const elementId = `param-${name}`;
    const dataAttr = escapeHtml(name);

    if (enumValues) {
      const options = ["<option value=''></option>"];
      for (const item of enumValues) {
        const selected = safeStr(defaultValue) === item ? " selected" : "";
        options.push(
          `<option value="${escapeHtml(item)}"${selected}>${escapeHtml(item)}</option>`
        );
      }

      return (
        `<div class="form-field">` +
        `<label for="${elementId}">${renderLabel(label, required)}</label>` +
        `<select id="${elementId}" data-param="${dataAttr}" class="input">` +
        options.join("") +
        `</select>` +
        `<div class="hint">${escapeHtml(description)}</div>` +
        `</div>`
      );
    }

    if (typ === "boolean") {
      const checked = !!defaultValue ? " checked" : "";
      return (
        `<div class="form-field checkbox-field">` +
        `<label class="checkbox-wrap">` +
        `<input type="checkbox" id="${elementId}" data-param="${dataAttr}"${checked}>` +
        `<span>${renderLabel(label, required)}</span>` +
        `</label>` +
        `<div class="hint">${escapeHtml(description)}</div>` +
        `</div>`
      );
    }

    const inputType = typ === "integer" || typ === "number" ? "number" : "text";
    const s = schema(param);
    const minAttr = s.minimum !== undefined ? ` min="${escapeHtml(String(s.minimum))}"` : "";
    const maxAttr = s.maximum !== undefined ? ` max="${escapeHtml(String(s.maximum))}"` : "";
    const placeholder = description ? escapeHtml(description.slice(0, 120)) : "";

    return (
      `<div class="form-field">` +
      `<label for="${elementId}">${renderLabel(label, required)}</label>` +
      `<input id="${elementId}" data-param="${dataAttr}" class="input" type="${inputType}" value="${valueAttr}" placeholder="${placeholder}"${minAttr}${maxAttr}>` +
      `<div class="hint">${escapeHtml(description)}</div>` +
      `</div>`
    );
  }

  function renderFormSection(titleText, params, collapsible = false, openByDefault = false) {
    if (!params.length) return "";
    const content = params.map(renderParamControl).join("");

    if (!collapsible) {
      return (
        `<h3>${escapeHtml(titleText)}</h3>` +
        `<div class="form-grid">` +
        `${content}` +
        `</div>`
      );
    }

    const openAttr = openByDefault ? " open" : "";
    return (
      `<details style="margin-top:16px;"${openAttr}>` +
      `<summary>${escapeHtml(titleText)}</summary>` +
      `<div class="form-grid" style="margin-top:12px;">` +
      `${content}` +
      `</div>` +
      `</details>`
    );
  }

  function buildDynamicForm(parameters) {
    const groupedParams = { search: [], analysis: [], advanced: [] };

    for (const p of parameters) {
      const name = safeStr(p.name);
      if (!name) continue;
      groupedParams[groupParameter(name)].push(p);
    }

    dynamicFormSections.innerHTML =
      renderFormSection(UI_TEXT.search_options || "Search options", groupedParams.search) +
      renderFormSection(UI_TEXT.analysis_options || "Analysis / cluster options", groupedParams.analysis, true, true) +
      renderFormSection(UI_TEXT.advanced || "Advanced", groupedParams.advanced, true, false);
  }

  function resolveOpenApiEndpointSpec(openapiSpec, endpointPath) {
    const paths = openapiSpec && typeof openapiSpec === "object" ? (openapiSpec.paths || {}) : {};
    const endpoint = paths[endpointPath] || {};
    const spec = endpoint.get || null;
    if (!spec || typeof spec !== "object") {
      throw new Error(`Endpoint spec for ${endpointPath} not found in OpenAPI`);
    }
    return spec;
  }

  function extractParameters(endpointSpec) {
    return Array.isArray(endpointSpec.parameters)
      ? endpointSpec.parameters.filter(p => p && typeof p === "object")
      : [];
  }

  function collectFormValues() {
    const params = new URLSearchParams();

    for (const param of ENDPOINT_PARAMETERS) {
      const name = safeStr(param.name);
      if (!name) continue;

      const el = document.querySelector(`[data-param="${CSS.escape(name)}"]`);
      if (!el) continue;

      const required = !!param.required;
      let value = "";

      if (el.type === "checkbox") {
        value = el.checked ? "true" : "false";
      } else {
        value = safeStr(el.value || "").trim();
      }

      if (!value && !required) continue;
      if (!value && required) {
        throw new Error(`Missing required parameter: ${name}`);
      }

      params.set(name, value);
    }

    params.set("format", "json");

    if (!params.get("analysis_mode")) params.set("analysis_mode", "both");
    if (!params.get("cluster_source")) params.set("cluster_source", "local");
    if (!params.get("cluster_label_source")) params.set("cluster_label_source", "local");

    return params;
  }

  function resetApiForm() {
    for (const param of ENDPOINT_PARAMETERS) {
      const name = safeStr(param.name);
      if (!name) continue;

      const el = document.querySelector(`[data-param="${CSS.escape(name)}"]`);
      if (!el) continue;

      const defaultValue = schemaDefault(param, "");

      if (el.type === "checkbox") {
        el.checked = !!defaultValue;
      } else {
        el.value = defaultValue !== undefined && defaultValue !== null ? String(defaultValue) : "";
      }
    }
  }

  function setHits(hits) {
    allPreparedHits = prepareHits(hits);
    currentPage = 1;

    window.ANALYSIS_SEARCH_EXPLORER_HITS = hits.slice();
    window.ANALYSIS_SEARCH_EXPLORER_PREPARED_HITS = allPreparedHits.slice();

    window.dispatchEvent(new CustomEvent("analysis-explorer:hits-updated", {
      detail: { hits: hits.slice() }
    }));
    window.dispatchEvent(new CustomEvent("analysis-explorer:prepared-hits-updated"));

    refreshView();
  }

  function getAggregations(data) {
    if (!data || typeof data !== "object") return {};
    if (data.aggregations && typeof data.aggregations === "object") return data.aggregations;
    if (data.aggs && typeof data.aggs === "object") return data.aggs;
    return {};
  }

  function getAggregationBuckets(agg) {
    if (!agg || typeof agg !== "object") return [];
    if (Array.isArray(agg.buckets)) return agg.buckets;
    if (agg.values && typeof agg.values === "object" && Array.isArray(agg.values.buckets)) return agg.values.buckets;
    return [];
  }

  function renderAggregationTable(name, agg) {
    const buckets = getAggregationBuckets(agg);
    if (!buckets.length) return "";

    const rows = buckets.map(bucket => (
      "<tr>" +
        `<td>${escapeHtml(safeStr(bucket.key_as_string ?? bucket.key ?? ""))}</td>` +
        `<td>${escapeHtml(safeStr(bucket.doc_count ?? ""))}</td>` +
      "</tr>"
    )).join("");

    const totalDocCount = safeStr(
      agg.doc_count ??
      (agg.values && agg.values.doc_count != null ? agg.values.doc_count : "")
    );

    return (
      `<section class="cluster-section">` +
        `<div class="cluster-header">` +
          `<h2>${escapeHtml(name)}</h2>` +
          `<div class="cluster-meta">${escapeHtml(totalDocCount)} docs, ${buckets.length} buckets</div>` +
        `</div>` +
        `<table class="mini-table">` +
          `<thead><tr><th>key</th><th>doc_count</th></tr></thead>` +
          `<tbody>${rows}</tbody>` +
        `</table>` +
      `</section>`
    );
  }

  function renderAggregations(data) {
    const container = document.getElementById("aggregationsContainer");
    if (!container) return;

    const aggregations = getAggregations(data);
    const entries = Object.entries(aggregations);

    if (!entries.length) {
      container.innerHTML = "";
      return;
    }

    const html = entries
      .map(([name, agg]) => renderAggregationTable(name, agg))
      .filter(Boolean)
      .join("");

    container.innerHTML = html;
  }

  async function runApiSearch(event) {
    if (event) event.preventDefault();

    let params;
    try {
      params = collectFormValues();
    } catch (err) {
      setApiStatus(`${UI_TEXT.error || "Error"}: ${err.message}`, "error");
      return;
    }

    const url = `${EXPLORER_CONFIG.apiBaseUrl || ""}${EXPLORER_CONFIG.endpointPath}?${params.toString()}`;

    requestUrlBoxEl.innerHTML = `<strong>${escapeHtml(UI_TEXT.request_url || "Request URL")}:</strong>`;
    requestUrlPreEl.textContent = url;
    requestUrlPreEl.classList.remove("hidden");

    setApiStatus(UI_TEXT.searching || "Searching...");
    submitApiSearchBtn.disabled = true;

    try {
      const response = await fetch(url, {
        method: "GET",
        headers: { "Accept": "application/json" }
      });

      const text = await response.text();
      let data = null;

      try {
        data = text ? JSON.parse(text) : null;
      } catch (_err) {
        throw new Error(`Response was not valid JSON (HTTP ${response.status})`);
      }

      setRawResponse(data);
      renderAggregations(data);

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const hits = normalizeHitsFromResponse(data);
      setHits(hits);
      setApiStatus(`${UI_TEXT.status_fetch_ok || "Search completed"}: ${hits.length} hits`);
    } catch (err) {
      setApiStatus(`${UI_TEXT.status_fetch_failed || "Search failed"}: ${err.message}`, "error");
    } finally {
      submitApiSearchBtn.disabled = false;
    }
  }
  function runAtlasSearch(event) {
    if (event) event.preventDefault();

    let params;
    try {
      params = collectFormValues();
    } catch (err) {
      setApiStatus(`${UI_TEXT.error || "Error"}: ${err.message}`, "error");
      return;
    }

    // wichtig: überschreibt json
    params.set("format", "atlas");

    const url = `${EXPLORER_CONFIG.apiBaseUrl || ""}${EXPLORER_CONFIG.endpointPath}?${params.toString()}`;

    requestUrlBoxEl.innerHTML = `<strong>${escapeHtml(UI_TEXT.request_url || "Request URL")}:</strong>`;
    requestUrlPreEl.textContent = url;
    requestUrlPreEl.classList.remove("hidden");

    const popup = window.open(url, "_blank", "noopener,noreferrer");

    if (!popup) {
      setApiStatus("Popup blocked – please allow popups", "error");
      return;
    }

    setApiStatus("Opening Atlas...");
  }
  async function loadOpenApiAndBuildForm() {
    setApiStatus(UI_TEXT.loading_openapi || "Loading OpenAPI...");
    submitApiSearchBtn.disabled = true;

    try {
      const response = await fetch(EXPLORER_CONFIG.openapiUrl, {
        method: "GET",
        headers: { "Accept": "application/json" }
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const openapiSpec = await response.json();
      const endpointSpec = resolveOpenApiEndpointSpec(openapiSpec, EXPLORER_CONFIG.endpointPath);
      ENDPOINT_PARAMETERS = extractParameters(endpointSpec);

      buildDynamicForm(ENDPOINT_PARAMETERS);
      resetApiForm();

      submitApiSearchBtn.disabled = false;
      setApiStatus(UI_TEXT.status_ready || "Ready");
    } catch (err) {
      dynamicFormSections.innerHTML = "";
      setApiStatus(`${UI_TEXT.status_openapi_failed || "OpenAPI load failed"}: ${err.message}`, "error");
    }
  }

  let filterDebounceTimer = null;

  function scheduleRefresh() {
    if (filterDebounceTimer) window.clearTimeout(filterDebounceTimer);
    filterDebounceTimer = window.setTimeout(function () {
      currentPage = 1;
      refreshView();
    }, 120);
  }

  function isMetaFacetBadge(buttonEl) {
    if (!buttonEl) return false;
    return buttonEl.classList.contains("meta-facet-badge") ||
           buttonEl.classList.contains("meta-active-filter");
  }

  document.addEventListener("click", function (event) {
    const btn = event.target.closest(".term-badge");
    if (!btn) return;

    if (isMetaFacetBadge(btn)) return;

    const rawTerm = safeStr(btn.dataset.term || "").trim();
    if (!rawTerm) return;

    const clickedTerm = normalize(rawTerm);
    if (!clickedTerm) return;

    if (activeTerm === clickedTerm) {
      setActiveTerm("");
    } else {
      setActiveTerm(clickedTerm);
    }
  });

  clientSearchInput.addEventListener("input", scheduleRefresh);

  clientClusterSelect.addEventListener("change", function () {
    currentPage = 1;
    refreshView();
  });

  resetClientFiltersBtn.addEventListener("click", function () {
    clientSearchInput.value = "";
    clientClusterSelect.value = "";
    setActiveTerm("");
  });

  expandAllBtn.addEventListener("click", function () {
    const details = Array.from(document.querySelectorAll(".details-block"));
    const shouldOpen = details.some(d => !d.open);
    details.forEach(d => {
      d.open = shouldOpen;
    });
  });

  apiSearchForm.addEventListener("submit", runApiSearch);
  if (atlasSearchBtn) {
    atlasSearchBtn.addEventListener("click", runAtlasSearch);
  }
  
  resetApiFormBtn.addEventListener("click", function () {
    resetApiForm();
    setApiStatus(UI_TEXT.status_ready || "Ready");
  });

  toggleRawResponseBtn.addEventListener("click", function () {
    rawResponseVisible = !rawResponseVisible;
    rawResponseEl.classList.toggle("visible", rawResponseVisible);
    toggleRawResponseBtn.textContent = rawResponseVisible
      ? (UI_TEXT.hide_raw_response || "Hide raw response")
      : (UI_TEXT.show_raw_response || "Show raw response");
  });

  initStaticTexts();
  renderAggregations({});

  if (EXPLORER_CONFIG.initialHits.length) {
    setHits(EXPLORER_CONFIG.initialHits);
    setApiStatus(`${UI_TEXT.status_loaded_initial_hits || "Loaded initial hits"}: ${EXPLORER_CONFIG.initialHits.length} hits`);
  } else {
    setHits([]);
    setApiStatus(UI_TEXT.status_no_initial_hits || "No initial hits loaded", "warning");
  }

  window.ANALYSIS_SEARCH_EXPLORER_REFRESH = function () {
    currentPage = 1;
    refreshView();
  };

  loadOpenApiAndBuildForm();
})();