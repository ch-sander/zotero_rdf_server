// Shared by the independent static pages. Keep request bodies in memory on this page only.
(() => {
  "use strict";

  const standaloneHosts = new Set(["data.scigma.de"]);
  const navigation = document.querySelector(".wk-nav");
  if (standaloneHosts.has(location.hostname) && navigation) {
    const inspector = navigation.querySelector("[data-show-queries]");
    const heading = document.querySelector(".wk-heading");
    if (inspector && heading) {
      inspector.remove();
      heading.append(inspector);
    }
    navigation.hidden = true;
  }

  const history = [];
  let current = [];
  let sequence = 0;
  let dialog;
  let picker;
  let destination;
  let output;
  let copy;
  let feedback;
  let options = [];
  let pending = false;

  function refreshIfOpen() {
    if (!dialog?.open || pending) return;
    pending = true;
    queueMicrotask(() => {
      pending = false;
      if (dialog.open) render(options[picker.selectedIndex]);
    });
  }

  function record(label, target, body) {
    const entry = { label, target, body, sequence: ++sequence };
    history.unshift(entry);
    if (history.length > 80) history.pop();
    refreshIfOpen();
    return entry;
  }

  function setCurrent(entries) {
    current = entries;
    refreshIfOpen();
  }

  function showEntry() {
    const entry = options[picker.selectedIndex];
    destination.textContent = entry?.target || "";
    const value = entry?.body ?? "";
    const body = typeof value === "function" ? value() : value;
    output.value = typeof body === "string" ? body : JSON.stringify(body, null, 2);
    copy.disabled = !output.value;
    feedback.textContent = "";
  }

  function render(selected) {
    const recent = history.filter(entry => !current.includes(entry));
    options = [...current, ...recent];
    picker.replaceChildren();
    let index = 0;
    for (const [name, entries] of [["Current query", current], ["Recent requests", recent]]) {
      if (!entries.length) continue;
      const group = document.createElement("optgroup");
      group.label = name;
      for (const entry of entries) {
        const prefix = entry.sequence ? "#" + entry.sequence + " · " : "";
        group.append(new Option(prefix + entry.label, String(index++)));
      }
      picker.append(group);
    }
    picker.disabled = !options.length;
    if (!options.length) picker.add(new Option("No queries yet", ""));
    picker.selectedIndex = Math.max(0, options.indexOf(selected));
    showEntry();
  }

  async function copyQuery() {
    const value = output.value;
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      output.focus();
      output.select();
      let copied = false;
      try { copied = document.execCommand("copy"); } catch { /* Keep the text selected. */ }
      if (!copied) {
        feedback.textContent = "Text selected. Use Ctrl+C or ⌘C to copy.";
        return;
      }
    }
    feedback.textContent = "Copied.";
  }

  function openInspector() {
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.id = "workspace-queries";
      dialog.className = "wk-dialog";
      dialog.setAttribute("aria-labelledby", "wk-dialog-title");
      dialog.innerHTML = `
        <div class="wk-dialog-head">
          <h2 id="wk-dialog-title">Query inspector</h2>
          <form method="dialog"><button class="wk-button" autofocus>Close</button></form>
        </div>
        <label class="wk-field">Query<select></select></label>
        <p class="wk-destination"></p>
        <textarea readonly spellcheck="false" wrap="off" aria-label="Full query"
          placeholder="Queries will appear here as you use this tool."></textarea>
        <div class="wk-dialog-foot"><span role="status"></span>
          <button class="wk-button wk-primary" type="button">Copy query</button>
        </div>`;
      document.body.append(dialog);
      picker = dialog.querySelector("select");
      destination = dialog.querySelector(".wk-destination");
      output = dialog.querySelector("textarea");
      copy = dialog.querySelector(".wk-dialog-foot button");
      feedback = dialog.querySelector("[role=status]");
      picker.addEventListener("change", showEntry);
      copy.addEventListener("click", copyQuery);
      dialog.addEventListener("click", event => {
        if (event.target !== dialog) return;
        const box = dialog.getBoundingClientRect();
        if (event.clientX < box.left || event.clientX > box.right ||
            event.clientY < box.top || event.clientY > box.bottom) dialog.close();
      });
    }
    render();
    dialog.showModal();
  }

  document.querySelectorAll("[data-show-queries]").forEach(button => {
    button.addEventListener("click", openInspector);
  });

  document.querySelector(".wk-skip")?.addEventListener("click", event => {
    // Both RDF browsers use the URL fragment as the selected resource.
    event.preventDefault();
    const main = document.getElementById("workspace-main");
    main.focus({ preventScroll: true });
    main.scrollIntoView({ block: "start" });
  });

  // A relative override belongs to the source page. Resolve it before crossing modules.
  function linkEndpoint(value) {
    try {
      const url = new URL(value, document.baseURI);
      return url.origin === location.origin ? url.pathname + url.search + url.hash : url.href;
    } catch { return value; }
  }

  for (const section of document.querySelectorAll("[data-small-disclosure]")) {
    if (matchMedia("(max-width: 760px)").matches) section.open = false;
  }

  const tabs = document.querySelector(".wk-tabs");
  const activeTab = tabs?.querySelector("a[aria-current]");
  if (activeTab && tabs.scrollWidth > tabs.clientWidth) {
    tabs.scrollLeft += activeTab.getBoundingClientRect().right - tabs.getBoundingClientRect().right;
  }

  const params = new URLSearchParams(location.search);
  const endpoint = params.get("endpoint");
  for (const link of document.querySelectorAll("a[data-rdf-tool], a[data-workspace-home]")) {
    const target = new URL(link.href);
    if (endpoint) target.searchParams.set("endpoint", linkEndpoint(endpoint));
    if (link.dataset.rdfTool === "filter" && params.has("lang")) {
      target.searchParams.set("lang", params.get("lang"));
    }
    if (link.dataset.rdfTool === "browse" && document.getElementById("resource-uri")) {
      for (const name of ["class", "ontology", "mode"]) {
        if (params.has(name)) target.searchParams.set(name, params.get(name));
      }
    }
    if (link.href !== target.href) link.href = target.href;
  }

  window.LibraryWorkspace = { record, setCurrent, linkEndpoint };
})();
