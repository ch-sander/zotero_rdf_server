
const scriptUrl = new URL(document.currentScript.src);
const partialsBaseUrl = new URL("./", scriptUrl);

async function loadPartialShadow(containerId, htmlFileName) {
  const container = document.getElementById(containerId);

  if (!container) {
    return;
  }

  try {
    const htmlUrl = new URL(htmlFileName, partialsBaseUrl);
    const cssUrl = new URL("styles.css", partialsBaseUrl);

    const [htmlResponse, cssResponse] = await Promise.all([
      fetch(htmlUrl),
      fetch(cssUrl)
    ]);

    if (!htmlResponse.ok) {
      return;
    }

    const html = await htmlResponse.text();
    const css = cssResponse.ok ? await cssResponse.text() : "";

    const shadow = container.attachShadow({ mode: "open" });

    shadow.innerHTML = `
      <style>
        ${css}
      </style>
      ${html}
    `;
    if (containerId === "custom-footer-container") {
      initCitationPopup(shadow);
    }
  } catch {
    // intentionally ignored
  }
}

async function initCitationPopup(root) {
  const popup = root.getElementById("citationPopup");
  const citationText = root.getElementById("citationText");
  const copyButton = root.getElementById("copyCitationButton");
  const closeButton = root.getElementById("closeCitationButton");

  if (!popup || !citationText || !copyButton || !closeButton) {
    return;
  }

  closeButton.addEventListener("click", () => {
    popup.hidden = true;
  });

  copyButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(citationText.textContent);
    } catch {
      // intentionally ignored
    }
  });

  const citationPaths = [
    "/app/plugins/citations/software.cff",
    "/app/plugins/citations/data.cff",    
  ];
  const hasStaticCitation = citationText.innerText.trim().length > 0;

  if (hasStaticCitation) {
    copyButton.hidden = false;
    popup.hidden = false;
    return;
  }
  try {
    const citationRequests = citationPaths.map(path => {
      const url = `/plugin/citation/render?path=${encodeURIComponent(path)}`;
      return fetch(url)
        .then(response => response.ok ? response.text() : "")
        .catch(() => "");
    });

    const citationTexts = await Promise.all(citationRequests);

    const text = citationTexts
      .map(t => t.trim())
      .filter(Boolean)
      .join("\n\n---\n\n");

    if (!text) {
      return;
    }

    citationText.textContent = text;
    copyButton.hidden = false;
    popup.hidden = false;
  } catch {
    // intentionally ignored
  }
}


loadPartialShadow("custom-header-container", "custom-header.html");
loadPartialShadow("custom-footer-container", "custom-footer.html");