// Both Sparnatural elements read their attributes as soon as the vendor bundle defines them.
(() => {
  "use strict";
  const component = document.querySelector("spar-natural, sparnatural-form");
  const params = new URLSearchParams(location.search);
  if (params.get("endpoint")) component.setAttribute("endpoint", params.get("endpoint"));
  if (component.matches("sparnatural-form") && params.get("lang")) {
    component.setAttribute("lang", params.get("lang"));
  }
  const endpoint = component.getAttribute("endpoint");
  const display = document.getElementById("displayEndpoint");
  display.textContent = endpoint;
  display.href = endpoint;

  // Observe the vendor transports on these two pages. Never consume request streams
  // or responses; the shipped widgets send query parameters, form bodies or SPARQL text.
  function recordRequest(rawUrl, method, body, headers) {
    try {
      let query;
      if (body instanceof URLSearchParams || (typeof FormData !== "undefined" && body instanceof FormData)) {
        query = body.get("query");
      } else if (typeof body === "string") {
        if (/(?:^|&)query=/.test(body)) query = new URLSearchParams(body).get("query");
        else if (/^\s*(?:#[^\n]*\n\s*)*(?:PREFIX|BASE|SELECT|ASK|CONSTRUCT|DESCRIBE)\b/i.test(body)) query = body;
      }
      if (!query && rawUrl.includes("query=")) query = new URL(rawUrl, document.baseURI).searchParams.get("query");
      if (typeof query !== "string" || !query) return;
      const target = new URL(rawUrl, document.baseURI);
      target.searchParams.delete("query");
      const accept = new Headers(headers).get("Accept") || "";
      const label = accept.includes("text/csv") ? "CSV export" : "SPARQL request";
      LibraryWorkspace.record(label, method + " " + target.href, query);
    } catch { /* Inspection must never interrupt the widget's request. */ }
  }

  const nativeFetch = window.fetch;
  window.fetch = function(input, init) {
    const result = nativeFetch.apply(this, arguments);
    const url = typeof input === "string" ? input : input?.url || input?.href || "";
    recordRequest(url, init?.method || input?.method || "GET", init?.body, init?.headers || input?.headers);
    return result;
  };

  // YASQE and the date widget also use XMLHttpRequest. A reused XHR gets a new URL on open.
  if (window.XMLHttpRequest) {
    const requests = new WeakMap();
    const proto = XMLHttpRequest.prototype;
    const open = proto.open;
    const send = proto.send;
    proto.open = function(method, url) {
      const result = open.apply(this, arguments);
      requests.set(this, { method, url: String(url) });
      return result;
    };
    proto.send = function(body) {
      const info = requests.get(this);
      if (info) recordRequest(info.url, info.method, body);
      return send.apply(this, arguments);
    };
  }

  function resourceLink(uri) {
    const target = new URL("../browse/resource/", document.baseURI);
    target.searchParams.set("endpoint", LibraryWorkspace.linkEndpoint(endpoint));
    target.hash = encodeURIComponent(uri);
    return target.href;
  }

  // Keep the editor and the result table usable if the original YASGUI CDN is unavailable.
  function plainViews(element) {
    const editor = document.createElement("textarea");
    editor.className = "plain-editor";
    editor.setAttribute("aria-label", "SPARQL editor");
    editor.spellcheck = false;
    const run = document.createElement("button");
    run.type = "button";
    run.className = "wk-button wk-primary";
    run.textContent = "Run SPARQL";
    document.getElementById("yasqe").append(editor, run);
    const host = document.getElementById("yasr");
    const note = document.createElement("p");
    note.className = "view-status";
    note.textContent = "YASGUI is unavailable. Results use the basic table view.";
    const result = document.createElement("div");
    host.append(note, result);
    function setResponse(response) {
      const data = response?.data ?? response;
      result.replaceChildren();
      if (!Array.isArray(data?.results?.bindings)) {
        result.textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2);
        return;
      }
      const bindings = data.results.bindings;
      if (!bindings.length) {
        result.textContent = "No matching records.";
        return;
      }
      const vars = data.head?.vars || [];
      const perPage = 100;
      function displayPage(start) {
        const table = document.createElement("table");
        const header = table.createTHead().insertRow();
        for (const name of vars) {
          const cell = document.createElement("th");
          cell.textContent = name;
          header.append(cell);
        }
        const rows = table.createTBody();
        for (const binding of bindings.slice(start, start + perPage)) {
          const row = rows.insertRow();
          for (const name of vars) {
            const term = binding[name];
            const cell = row.insertCell();
            if (term?.type === "uri") {
              const a = document.createElement("a");
              a.href = resourceLink(term.value);
              a.textContent = term.value;
              cell.append(a);
            } else cell.textContent = term?.value || "";
          }
        }
        result.replaceChildren(table);
        if (bindings.length <= perPage) return;
        const controls = document.createElement("div");
        controls.className = "builder-actions";
        controls.append(document.createTextNode(`${start + 1}–${Math.min(start + perPage, bindings.length)} of ${bindings.length}`));
        for (const [name, next] of [["Previous", start - perPage], ["Next", start + perPage]]) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "wk-button";
          button.textContent = name;
          button.disabled = next < 0 || next >= bindings.length;
          button.addEventListener("click", () => displayPage(next));
          controls.append(button);
        }
        result.append(controls);
      }
      displayPage(0);
    }
    run.addEventListener("click", () => {
      if (!editor.value.trim()) return;
      element.executeSparql(editor.value, setResponse, error => setResponse(String(error)));
    });
    return {
      yasqe: { getValue: () => editor.value, setValue: value => { editor.value = value; }, refresh() {} },
      yasr: { plugins: {}, setResponse }
    };
  }

  function createViews() {
    let views;
    if (typeof Yasqe === "function" && typeof Yasr === "function" && window.SparnaturalYasguiPlugins) {
      const yasqe = new Yasqe(document.getElementById("yasqe"), {
        requestConfig: { endpoint, method: "GET", header: {} },
        copyEndpointOnNewTab: false
      });
      const plugins = SparnaturalYasguiPlugins;
      const names = { TableX: plugins.TableX, Grid: plugins.GridPlugin, Stats: plugins.StatsPlugin, Map: plugins.MapPlugin };
      for (const [name, plugin] of Object.entries(names)) Yasr.registerPlugin(name, plugin);
      delete Yasr.plugins.table;
      delete Yasr.plugins.response;
      const yasr = new Yasr(document.getElementById("yasr"), {
        pluginOrder: Object.keys(names), defaultPlugin: "TableX",
        persistencyExpire: 0, maxPersistentResponseSize: 0
      });
      const table = yasr.plugins.TableX;
      Object.assign(table.config, { includeControls: true, openIriInNewWindow: true, uriHrefAdapter: resourceLink });
      table.persistentConfig.compact = true;
      yasqe.on("queryResponse", (_editor, response, duration) => {
        yasr.setResponse(response, duration);
        component.enablePlayBtn();
      });
      views = { yasqe, yasr };
    } else views = plainViews(component);

    const editorBox = document.getElementById("yasqe");
    const toggle = document.getElementById("sparql-toggle");
    toggle.addEventListener("click", () => {
      editorBox.hidden = !editorBox.hidden;
      toggle.setAttribute("aria-expanded", String(!editorBox.hidden));
      toggle.textContent = editorBox.hidden ? "Show SPARQL editor" : "Hide SPARQL editor";
      if (!editorBox.hidden) views.yasqe.refresh();
    });
    const draft = { label: "SPARQL editor", target: endpoint, get body() { return views.yasqe.getValue(); } };
    const currentDraft = [draft];
    const refreshDraft = () => LibraryWorkspace.setCurrent(currentDraft);
    component.addEventListener("queryUpdated", () => queueMicrotask(refreshDraft));
    component.addEventListener("reset", () => queueMicrotask(refreshDraft));
    refreshDraft();
    return views;
  }

  window.SparqlWorkspace = { createViews };
})();
