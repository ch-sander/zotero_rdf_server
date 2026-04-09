(function () {
  "use strict";

  const ROOT_CONFIG = window.ANALYSIS_SEARCH_EXPLORER_CONFIG || {};
  const META_CONFIG = ROOT_CONFIG.metaFacets || {};

  const state = {
    facetCounts: new Map(),
    selected: new Map()
  };

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

  function normalize(value) {
    return safeStr(value).trim();
  }

  function getPreparedHits() {
    return Array.isArray(window.ANALYSIS_SEARCH_EXPLORER_PREPARED_HITS)
      ? window.ANALYSIS_SEARCH_EXPLORER_PREPARED_HITS
      : [];
  }

  function rebuildFacetCountsFromPreparedHits() {
    state.facetCounts = new Map();

    const hits = getPreparedHits();
    for (const hit of hits) {
      const facets = hit && hit.metaFacets && typeof hit.metaFacets === "object"
        ? hit.metaFacets
        : {};

      for (const [field, values] of Object.entries(facets)) {
        if (!state.facetCounts.has(field)) {
          state.facetCounts.set(field, new Map());
        }

        const fieldMap = state.facetCounts.get(field);
        for (const value of values) {
          fieldMap.set(value, (fieldMap.get(value) || 0) + 1);
        }
      }
    }
  }

  function matchesPreparedHit(item) {
    if (!state.selected.size) return true;

    const facets = item && item.metaFacets && typeof item.metaFacets === "object"
      ? item.metaFacets
      : {};

    for (const [field, selectedValues] of state.selected.entries()) {
      const docValues = Array.isArray(facets[field]) ? facets[field] : [];
      const ok = Array.from(selectedValues).some(v => docValues.includes(v));
      if (!ok) return false;
    }

    return true;
  }

  window.ANALYSIS_SEARCH_EXPLORER_META_STATE = {
    matchesPreparedHit
  };

  function renderActiveFilters() {
    const el = document.getElementById("activeMetaFacets");
    if (!el) return;

    const parts = [];

    for (const [field, values] of state.selected.entries()) {
      for (const value of values) {
        parts.push(
          `<button class="term-badge meta-active-filter active-term" data-field="${escapeHtml(field)}" data-value="${escapeHtml(value)}" type="button">` +
            `${escapeHtml(field)}: ${escapeHtml(value)}` +
          `</button>`
        );
      }
    }

    el.innerHTML = parts.length
      ? parts.join("")
      : `<span class="muted">No active meta filters</span>`;
  }

  function renderFacets() {
    const root = document.getElementById("metaFacets");
    if (!root) return;

    const openByDefault = META_CONFIG.openByDefault === true;
    const openAttr = openByDefault ? " open" : "";

    const fields = Array.from(state.facetCounts.keys()).sort((a, b) => a.localeCompare(b));

    if (!fields.length) {
      root.innerHTML = `<div class="muted">No meta facets</div>`;
      renderActiveFilters();
      return;
    }

    root.innerHTML = fields.map(field => {
      const buckets = Array.from(state.facetCounts.get(field).entries())
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));

      const selected = state.selected.get(field) || new Set();

      const buttons = buckets.map(([value, count]) => {
        const activeClass = selected.has(value) ? " active-term" : "";
        return (
          `<button class="term-badge meta-facet-badge${activeClass}" data-field="${escapeHtml(field)}" data-value="${escapeHtml(value)}" type="button">` +
            `${escapeHtml(value)} <span class="muted">(${count})</span>` +
          `</button>`
        );
      }).join("");

      return (
        `<details class="meta-facet-group"${openAttr}>` +
          `<summary><strong>${escapeHtml(field)}</strong> <span class="muted">(${buckets.length})</span></summary>` +
          `<div class="term-cloud" style="margin-top:8px;">${buttons}</div>` +
        `</details>`
      );
    }).join("");

    renderActiveFilters();
  }

  function requestRefresh() {
    if (typeof window.ANALYSIS_SEARCH_EXPLORER_REFRESH === "function") {
      window.ANALYSIS_SEARCH_EXPLORER_REFRESH();
    }
  }

  function toggleFacet(field, value) {
    if (!field || !value) return;

    if (!state.selected.has(field)) {
      state.selected.set(field, new Set());
    }

    const set = state.selected.get(field);

    if (set.has(value)) {
      set.delete(value);
      if (!set.size) state.selected.delete(field);
    } else {
      set.add(value);
    }

    renderFacets();
    requestRefresh();
  }

  function resetFacets() {
    state.selected = new Map();
    renderFacets();
    requestRefresh();
  }

  function toggleAllFacetGroups() {
    const groups = Array.from(document.querySelectorAll(".meta-facet-group"));
    if (!groups.length) return;

    const shouldOpen = groups.some(g => !g.open);
    groups.forEach(g => {
      g.open = shouldOpen;
    });
  }

  function syncFromPreparedHits() {
    rebuildFacetCountsFromPreparedHits();

    for (const [field, selectedValues] of Array.from(state.selected.entries())) {
      const available = state.facetCounts.get(field);
      if (!available) {
        state.selected.delete(field);
        continue;
      }

      for (const value of Array.from(selectedValues)) {
        if (!available.has(value)) {
          selectedValues.delete(value);
        }
      }

      if (!selectedValues.size) {
        state.selected.delete(field);
      }
    }

    renderFacets();
  }

  document.addEventListener("click", function (event) {
    const facetBtn = event.target.closest(".meta-facet-badge");
    if (facetBtn) {
      toggleFacet(
        normalize(facetBtn.dataset.field),
        normalize(facetBtn.dataset.value)
      );
      return;
    }

    const activeBtn = event.target.closest(".meta-active-filter");
    if (activeBtn) {
      toggleFacet(
        normalize(activeBtn.dataset.field),
        normalize(activeBtn.dataset.value)
      );
      return;
    }

    if (event.target.closest("#resetMetaFacetsBtn")) {
      resetFacets();
      return;
    }

    if (event.target.closest("#toggleAllMetaFacetsBtn")) {
      toggleAllFacetGroups();
    }
  });

  window.addEventListener("analysis-explorer:prepared-hits-updated", syncFromPreparedHits);

  syncFromPreparedHits();
})();