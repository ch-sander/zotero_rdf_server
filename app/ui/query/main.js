"use strict";

const sparnatural = document.querySelector("spar-natural");
const { yasqe, yasr } = SparqlWorkspace.createViews();

bindSparnaturalWithYasrPlugins(sparnatural, yasr);
bindSparnaturalWithItself(sparnatural, yasqe, yasr);

const exportModal = document.getElementById("exportModal");
const importModal = document.getElementById("importModal");
const importError = document.getElementById("import-error");

document.getElementById("export").addEventListener("click", () => {
  const current = document.getElementById("query-json").value;
  document.getElementById("export-json").value = current ? JSON.stringify(JSON.parse(current), null, 2) : "";
  exportModal.showModal();
});

document.getElementById("import").addEventListener("click", () => {
  importError.textContent = "";
  importModal.showModal();
});

document.getElementById("importButton").addEventListener("click", () => {
  try {
    const query = JSON.parse(document.getElementById("import-json").value);
    sparnatural.loadQuery(query);
    importModal.close();
  } catch (error) {
    importError.textContent = error.message;
  }
});
