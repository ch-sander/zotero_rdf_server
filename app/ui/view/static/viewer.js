"use strict";


const configElement =
  document.getElementById("viewer-config");

const config = JSON.parse(
  configElement.textContent
);

const sourceUrl = config.sourceUrl;
const prefixUrl = config.prefixUrl;


let viewer = null;
let viewerPageCount = 0;


/*
 * Return the ID of a IIIF Image service.
 */
function serviceId(service) {
  if (!service) {
    return null;
  }

  if (Array.isArray(service)) {
    for (const entry of service) {
      const id = serviceId(entry);

      if (id) {
        return id;
      }
    }

    return null;
  }

  return (
    service.id
    || service["@id"]
    || null
  );
}


/*
 * Convert a IIIF Image API service ID to info.json.
 */
function infoJson(service) {
  const id = serviceId(service);

  if (!id) {
    return null;
  }

  if (id.endsWith("/info.json")) {
    return id;
  }

  return (
    id.replace(/\/$/, "")
    + "/info.json"
  );
}


/*
 * Extract image services from a
 * IIIF Presentation API 2.x manifest.
 */
function iiif2Sources(manifest) {
  const canvases =
    manifest.sequences?.[0]?.canvases
    || [];

  return canvases
    .map(canvas => {
      const resource =
        canvas.images?.[0]?.resource;

      return infoJson(
        resource?.service
      );
    })
    .filter(Boolean);
}


/*
 * Extract image services from a
 * IIIF Presentation API 3.x manifest.
 */
function iiif3Sources(manifest) {
  const sources = [];

  for (const canvas of manifest.items || []) {

    let source = null;

    for (
      const annotationPage
      of canvas.items || []
    ) {

      for (
        const annotation
        of annotationPage.items || []
      ) {

        const body = annotation.body;

        source = infoJson(
          body?.service
        );

        if (source) {
          break;
        }
      }

      if (source) {
        break;
      }
    }

    if (source) {
      sources.push(source);
    }
  }

  return sources;
}


/*
 * Detect a direct IIIF Image API response.
 */
function isIiifImageInfo(data) {
  if (!data) {
    return false;
  }

  if (
    data.protocol ===
    "http://iiif.io/api/image"
  ) {
    return true;
  }

  const context = data["@context"];

  if (typeof context === "string") {
    return context.includes(
      "iiif.io/api/image"
    );
  }

  if (Array.isArray(context)) {
    return context.some(entry =>
      String(entry).includes(
        "iiif.io/api/image"
      )
    );
  }

  return false;
}


/*
 * Load _input and return OpenSeadragon
 * tile sources if it is usable as IIIF.
 */
async function loadIiifSources(url) {
  if (!url) {
    return [];
  }

  const response = await fetch(url);

  if (!response.ok) {
    throw new Error(
      `Source request failed: ${response.status}`
    );
  }

  const data = await response.json();


  /*
   * Direct Image API info.json.
   */
  if (isIiifImageInfo(data)) {
    return [url];
  }


  /*
   * Presentation API 2.x.
   */
  if (data.sequences) {
    return iiif2Sources(data);
  }


  /*
   * Presentation API 3.x.
   */
  if (data.items) {
    return iiif3Sources(data);
  }


  return [];
}


/*
 * Parse URLs such as:
 *
 *     document.html#page=17
 *
 * Return null when no page was explicitly
 * requested. This keeps the document at
 * the metadata header on normal page load.
 */
function formatPage(page) {
  const value = Number(page);

  if (
    !Number.isSafeInteger(value)
    || value <= 0
  ) {
    return null;
  }

  return String(value).padStart(4, "0");
}


function pageFromUrl() {
  const hash = window.location.hash.slice(1);

  if (!hash) {
    return null;
  }

  const params = new URLSearchParams(hash);
  const formattedPage = formatPage(params.get("page"));

  if (formattedPage === null) {
    return null;
  }

  return Number(formattedPage);
}


/*
 * Update the URL without creating a new
 * browser history entry for every page turn.
 */
function updatePageUrl(page) {
  const formattedPage = formatPage(page);

  if (formattedPage === null) {
    return;
  }

  history.replaceState(
    null,
    "",
    `#page=${formattedPage}`,
  );
}


/*
 * Remove the active state from all OCR pages.
 */
function clearActivePages() {
  document
    .querySelectorAll(".page.active")
    .forEach(element => {
      element.classList.remove(
        "active"
      );
    });
}


/*
 * Highlight one OCR page without scrolling.
 */
function highlightTextPage(page) {
  clearActivePages();

  const section =
    document.querySelector(
      `[data-page="${page}"]`
    );

  if (!section) {
    return;
  }

  section.classList.add(
    "active"
  );
}


/*
 * Highlight and scroll to an OCR page.
 */
function scrollToTextPage(
  page,
  behavior = "smooth"
) {
  const section =
    document.querySelector(
      `[data-page="${page}"]`
    );

  if (!section) {
    return;
  }

  highlightTextPage(page);

  section.scrollIntoView({
    behavior,
    block: "start"
  });
}


/*
 * Navigate to one logical page in both
 * the OCR text and OpenSeadragon.
 */
function goToPage(
  page,
  {
    scrollText = true,
    updateUrl = false,
    behavior = "smooth"
  } = {}
) {
  if (
    !Number.isInteger(page)
    || page < 1
  ) {
    return;
  }

  if (scrollText) {
    scrollToTextPage(
      page,
      behavior
    );
  } else {
    highlightTextPage(page);
  }

  if (
    viewer
    && page <= viewerPageCount
    && viewer.currentPage() !== page - 1
  ) {
    /*
     * OpenSeadragon page indices
     * are zero-based.
     */
    viewer.goToPage(
      page - 1
    );
  }

  if (updateUrl) {
    updatePageUrl(page);
  }
}


/*
 * Collapse or show the image viewer.
 */
function setupViewerToggle() {
  const button =
    document.getElementById(
      "viewer-toggle"
    );

  const panel =
    document.getElementById(
      "viewer-panel"
    );

  if (!button || !panel) {
    return;
  }

  button.addEventListener(
    "click",
    () => {
      const collapsed =
        document.body.classList.toggle(
          "viewer-collapsed"
        );

      button.setAttribute(
        "aria-expanded",
        String(!collapsed)
      );

      button.textContent =
        collapsed
          ? "Show viewer"
          : "Hide viewer";

      /*
       * OpenSeadragon normally notices
       * container resizing, but forcing a
       * viewport update is useful after the
       * hidden panel becomes visible again.
       */
      if (
        !collapsed
        && viewer
      ) {
        requestAnimationFrame(() => {
          viewer.viewport.applyConstraints(
            true
          );
        });
      }
    }
  );
}


/*
 * Hide the viewer when _input is not a
 * usable IIIF source.
 */
function disableViewer(message) {
  const panel =
    document.getElementById(
      "viewer-panel"
    );

  const button =
    document.getElementById(
      "viewer-toggle"
    );

  const messageElement =
    document.getElementById(
      "viewer-message"
    );

  if (messageElement) {
    messageElement.textContent =
      message;

    messageElement.hidden =
      false;
  }

  document.body.classList.add(
    "viewer-collapsed"
  );

  if (panel) {
    panel.setAttribute(
      "aria-hidden",
      "true"
    );
  }

  if (button) {
    button.hidden = true;
  }
}


/*
 * Initialise OpenSeadragon only when
 * _input resolves to IIIF.
 */
async function startViewer() {
  if (!sourceUrl) {
    disableViewer(
      "No IIIF source available."
    );

    return;
  }


  let tileSources;

  try {
    tileSources =
      await loadIiifSources(
        sourceUrl
      );
  } catch (error) {
    console.info(
      "Source could not be loaded as IIIF:",
      error
    );

    disableViewer(
      "No IIIF viewer available for this source."
    );

    return;
  }


  if (!tileSources.length) {
    disableViewer(
      "This source does not provide a IIIF Image API service."
    );

    return;
  }


  viewerPageCount =
    tileSources.length;


  document.body.classList.remove(
    "viewer-collapsed"
  );


  viewer = OpenSeadragon({
    id: "viewer",

    prefixUrl,

    tileSources,

    sequenceMode: true,

    showReferenceStrip: true,

    preserveViewport: true,

    showNavigator: true
  });


  /*
   * A normal document load must stay at
   * the metadata header.
   *
   * Only navigate when #page=N was
   * explicitly supplied in the URL.
   */
  viewer.addHandler(
    "open",
    () => {
      const page =
        pageFromUrl();

      if (page !== null) {
        goToPage(
          page,
          {
            scrollText: true,
            updateUrl: false,
            behavior: "auto"
          }
        );
      }
    }
  );


  /*
   * When the user changes the image page,
   * synchronize OCR and URL.
   */
  viewer.addHandler(
    "page",
    event => {
      const page =
        event.page + 1;

      updatePageUrl(page);

      scrollToTextPage(
        page
      );
    }
  );
}


/*
 * Page links inside the OCR text use the
 * URL hash. Handle browser back/forward
 * and manually entered #page=N links.
 */
window.addEventListener(
  "hashchange",
  () => {
    const page =
      pageFromUrl();

    if (page === null) {
      return;
    }

    goToPage(
      page,
      {
        scrollText: true,
        updateUrl: false
      }
    );
  }
);


setupViewerToggle();


startViewer()
  .catch(error => {
    console.error(
      "Viewer initialisation failed:",
      error
    );

    disableViewer(
      "The IIIF viewer could not be initialised."
    );
  })
  .finally(() => {
    /*
     * Direct OCR links also work when no
     * OpenSeadragon viewer is available.
     *
     * Crucially: do nothing on a normal
     * document URL without #page=N.
     */
    const page =
      pageFromUrl();

    if (
      page !== null
      && !viewer
    ) {
      scrollToTextPage(
        page,
        "auto"
      );
    }
  });