
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
  } catch {
    // intentionally ignored
  }
}

loadPartialShadow("custom-header-container", "custom-header.html");
loadPartialShadow("custom-footer-container", "custom-footer.html");