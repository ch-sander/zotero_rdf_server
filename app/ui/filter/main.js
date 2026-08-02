//
// Sparnatural Form integration for the Zotero RDF Server
//

"use strict";

// Referenz auf die Sparnatural-Form-Komponente
const sparnatural = document.querySelector("sparnatural-form");

if (!sparnatural) {
  throw new Error("Kein <sparnatural-form>-Element gefunden.");
}

// Endpoint und Sprache können über URL-Parameter überschrieben werden.
// Beispiel:
// index.html?endpoint=https%3A%2F%2Fexample.org%2Fsparql&lang=de
const params = new URLSearchParams(window.location.search);

const endpointFromUrl = params.get("endpoint");
const languageFromUrl = params.get("lang");

const endpoint =
  endpointFromUrl ||
  sparnatural.getAttribute("endpoint");

const language =
  languageFromUrl ||
  sparnatural.getAttribute("lang") ||
  "de";

sparnatural.setAttribute("endpoint", endpoint);
sparnatural.setAttribute("lang", language);

// Endpoint auf der Seite anzeigen
const displayEndpoint = document.querySelector("#displayEndpoint");

if (displayEndpoint) {
  displayEndpoint.setAttribute("href", endpoint);
  displayEndpoint.setAttribute("target", "_blank");
  displayEndpoint.textContent = endpoint;
}

// Die in form.json definierten onscreen-Variablen laden.
// Die normale Suche enthält nur diese Variablen.
// Der Export enthält alle Variablen aus query.json.
const formConfigUrl = sparnatural.getAttribute("form");

const onscreenVariablesPromise = fetch(formConfigUrl)
  .then((response) => {
    if (!response.ok) {
      throw new Error(
        `form.json konnte nicht geladen werden: ${response.status}`
      );
    }

    return response.json();
  })
  .then((formConfig) => {
    return new Set(formConfig.variables?.onscreen || []);
  })
  .catch((error) => {
    console.warn(
      "onscreen-Variablen konnten nicht aus form.json gelesen werden:",
      error
    );

    return new Set();
  });

let latestQueryString = "";
let latestQueryJson = null;

// YASQE initialisieren
const yasqe = new Yasqe(document.getElementById("yasqe"), {
  requestConfig: {
    endpoint,
    method: "GET",
    header: {}
  },
  copyEndpointOnNewTab: false
});

// YASR-Plugins registrieren
Yasr.registerPlugin(
  "TableX",
  SparnaturalYasguiPlugins.TableX
);

Yasr.registerPlugin(
  "Grid",
  SparnaturalYasguiPlugins.GridPlugin
);

Yasr.registerPlugin(
  "Stats",
  SparnaturalYasguiPlugins.StatsPlugin
);

Yasr.registerPlugin(
  "Map",
  SparnaturalYasguiPlugins.MapPlugin
);

delete Yasr.plugins.table;
delete Yasr.plugins.response;

// YASR initialisieren
const yasr = new Yasr(
  document.getElementById("yasr"),
  {
    pluginOrder: [
      "TableX",
      "Grid",
      "Stats",
      "Map"
    ],
    defaultPlugin: "TableX",
    persistencyExpire: 0,
    maxPersistentResponseSize: 0
  }
);

// TableX konfigurieren
const tableXConfig = yasr.plugins.TableX;

if (tableXConfig) {
  Object.assign(tableXConfig.config, {
    includeControls: true,
    openIriInNewWindow: true,

    uriHrefAdapter: (uri) => {
      const targetUrl = new URL(
        "/ui/browse/resource/",
        window.location.origin
      );

      targetUrl.searchParams.set(
        "endpoint",
        endpoint
      );

      targetUrl.hash = encodeURIComponent(uri);

      return targetUrl.toString();
    }
  });

  tableXConfig.persistentConfig.compact = true;
}

// Sparnatural-Konfiguration an YASR-Plugins übergeben
sparnatural.addEventListener("init", (event) => {
  const configuration =
    event.detail?.config ||
    sparnatural.sparnaturalForm?.specProvider;

  if (!configuration) {
    return;
  }

  for (const pluginName in yasr.plugins) {
    const plugin = yasr.plugins[pluginName];

    if (plugin.notifyConfiguration) {
      plugin.notifyConfiguration(configuration);
    }
  }
});

// Query bei Änderungen aktualisieren
sparnatural.addEventListener(
  "queryUpdated",
  (event) => {
    latestQueryString = sparnatural.expandSparql(
      event.detail.queryString
    );

    latestQueryJson = event.detail.queryJson;

    yasqe.setValue(latestQueryString);

    if (
      sparnatural.getAttribute("debug") === "true"
    ) {
      console.log(
        "Sparnatural Form Query JSON:"
      );

      console.dir(latestQueryJson);
    }

    for (const pluginName in yasr.plugins) {
      const plugin = yasr.plugins[pluginName];

      if (plugin.notifyQuery) {
        plugin.notifyQuery(latestQueryJson);
      }
    }
  }
);

// Variablennamen aus dem Query-JSON lesen
function selectedVariableNames(queryJson) {
  return (queryJson?.variables || [])
    .map((variable) => {
      if (typeof variable === "string") {
        return variable;
      }

      return variable?.value;
    })
    .filter(Boolean);
}

// Prüfen, ob eine Variable zu einer onscreen-Variable gehört
function belongsToOnscreenVariable(
  variableName,
  onscreenVariables
) {
  if (onscreenVariables.has(variableName)) {
    return true;
  }

  // Sparnatural kann zusätzliche Label- oder Key-Info-
  // Variablen erzeugen.
  return Array.from(onscreenVariables).some(
    (baseName) => {
      return variableName.startsWith(
        `${baseName}_`
      );
    }
  );
}

// Erkennen, ob der Submit vom Export-Button stammt
async function isExportSubmission(event) {
  // Falls eine Sparnatural-Version den Modus direkt
  // im Event übergibt
  const explicitMode = String(
    event.detail?.mode ||
    event.detail?.type ||
    event.detail?.action ||
    event.detail?.queryType ||
    ""
  ).toLowerCase();

  if (
    explicitMode.includes("export") ||
    explicitMode.includes("csv")
  ) {
    return true;
  }

  if (
    explicitMode.includes("screen") ||
    explicitMode.includes("search")
  ) {
    return false;
  }

  // Fallback:
  // Die Exportquery enthält Variablen, die nicht
  // unter variables.onscreen stehen.
  const onscreenVariables =
    await onscreenVariablesPromise;

  if (onscreenVariables.size === 0) {
    return false;
  }

  const selectedVariables =
    selectedVariableNames(latestQueryJson);

  return selectedVariables.some(
    (variableName) => {
      return !belongsToOnscreenVariable(
        variableName,
        onscreenVariables
      );
    }
  );
}

// SPARQL-JSON bei Bedarf in CSV konvertieren
function sparqlJsonToCsv(result) {
  const variables =
    result?.head?.vars || [];

  const bindings =
    result?.results?.bindings || [];

  const escapeCsv = (value) => {
    const text = String(value ?? "");

    if (/[",\r\n]/.test(text)) {
      return `"${text.replace(/"/g, '""')}"`;
    }

    return text;
  };

  const lines = [
    variables.map(escapeCsv).join(",")
  ];

  for (const row of bindings) {
    const values = variables.map(
      (variable) => {
        return escapeCsv(
          row[variable]?.value || ""
        );
      }
    );

    lines.push(values.join(","));
  }

  return lines.join("\r\n");
}

// CSV vom SPARQL-Endpoint laden
async function requestCsv(queryString) {
  const endpointUrl = new URL(
    endpoint,
    window.location.href
  );

  const acceptHeader = [
    "text/csv",
    "application/sparql-results+csv;q=0.9",
    "application/sparql-results+json;q=0.5"
  ].join(", ");

  endpointUrl.searchParams.set(
    "query",
    queryString
  );

  let response = await fetch(endpointUrl, {
    method: "GET",
    headers: {
      Accept: acceptHeader
    }
  });

  // Falls GET nicht erlaubt oder URL zu lang ist
  if (
    response.status === 414 ||
    response.status === 405
  ) {
    const postUrl = new URL(
      endpoint,
      window.location.href
    );

    response = await fetch(postUrl, {
      method: "POST",
      headers: {
        Accept: acceptHeader,
        "Content-Type":
          "application/x-www-form-urlencoded;charset=UTF-8"
      },
      body: new URLSearchParams({
        query: queryString
      })
    });
  }

  if (!response.ok) {
    const message = await response.text();

    throw new Error(
      `CSV-Export fehlgeschlagen (${response.status}): ` +
      message.slice(0, 500)
    );
  }

  const contentType = (
    response.headers.get("content-type") || ""
  ).toLowerCase();

  // Einige Endpoints ignorieren den CSV-Accept-Header
  // und liefern SPARQL Results JSON.
  if (contentType.includes("json")) {
    const result = await response.json();

    const csv = sparqlJsonToCsv(result);

    return new Blob(
      [
        "\ufeff",
        csv
      ],
      {
        type: "text/csv;charset=utf-8"
      }
    );
  }

  const resultBlob = await response.blob();

  return new Blob(
    [
      "\ufeff",
      resultBlob
    ],
    {
      type: "text/csv;charset=utf-8"
    }
  );
}

// Browserdownload starten
function downloadBlob(blob, filename) {
  const downloadUrl =
    URL.createObjectURL(blob);

  const link =
    document.createElement("a");

  link.href = downloadUrl;
  link.download = filename;
  link.style.display = "none";

  document.body.appendChild(link);

  link.click();
  link.remove();

  // Nicht unmittelbar widerrufen, da einige Browser
  // den Download sonst abbrechen.
  window.setTimeout(() => {
    URL.revokeObjectURL(downloadUrl);
  }, 1000);
}

// Dateiname für den Export erzeugen
function exportFilename() {
  const timestamp = new Date()
    .toISOString()
    .replace(/[:.]/g, "-");

  return `zotero-export-${timestamp}.csv`;
}

// Fehler in YASR anzeigen
function showQueryError(message) {
  yasr.setResponse({
    contentType: "text/html",
    data: message,
    status: 500
  });
}

// Suche und Export lösen beide das submit-Event aus
sparnatural.addEventListener(
  "submit",
  async (event) => {
    const queryString =
      event.detail?.queryString
        ? sparnatural.expandSparql(
            event.detail.queryString
          )
        : latestQueryString ||
          yasqe.getValue().trim();

    if (!queryString) {
      console.warn(
        "Keine SPARQL-Query vorhanden."
      );

      return;
    }

    sparnatural.disablePlayBtn();

    let exportSubmission = false;

    try {
      exportSubmission =
        await isExportSubmission(event);

      if (exportSubmission) {
        const csvBlob =
          await requestCsv(queryString);

        downloadBlob(
          csvBlob,
          exportFilename()
        );

        return;
      }

      // Normale Bildschirmabfrage
      sparnatural.executeSparql(
        queryString,

        (response) => {
          yasr.setResponse(response);
          sparnatural.enablePlayBtn();
        },

        (error) => {
          console.error(
            "SPARQL-Abfrage fehlgeschlagen:",
            error
          );

          showQueryError(
            "Die Suchergebnisse konnten nicht geladen werden."
          );

          sparnatural.enablePlayBtn();
        }
      );
    } catch (error) {
      console.error(
        "Export fehlgeschlagen:",
        error
      );

      showQueryError(
        `Export fehlgeschlagen: ${error.message}`
      );
    } finally {
      // executeSparql arbeitet mit Callbacks und aktiviert
      // den Button dort wieder.
      // Beim CSV-Export muss dies hier geschehen.
      if (exportSubmission) {
        sparnatural.enablePlayBtn();
      }
    }
  }
);

// Editor beim Zurücksetzen leeren
function resetEditor() {
  latestQueryString = "";
  latestQueryJson = null;

  yasqe.setValue("");
}

sparnatural.addEventListener(
  "reset",
  resetEditor
);

sparnatural.addEventListener(
  "resetEditor",
  resetEditor
);

// SPARQL-Editor ein- und ausblenden
const sparqlToggle =
  document.getElementById("sparql-toggle");

if (sparqlToggle) {
  sparqlToggle.onclick = function () {
    const yasqeElement =
      document.getElementById("yasqe");

    if (
      yasqeElement.style.display === "none"
    ) {
      yasqeElement.style.display = "block";
      yasqe.refresh();
    } else {
      yasqeElement.style.display = "none";
    }

    return false;
  };
}