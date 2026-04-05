document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("viewer-root");
  if (!root) return;

  const pageSelect = document.getElementById("page-select");
  if (pageSelect) {
    pageSelect.addEventListener("change", () => {
      if (pageSelect.value) {
        window.location.href = pageSelect.value;
      }
    });
  }

  const configEl = document.getElementById("viewer-config");
  let config = {};
  if (configEl) {
    try {
      config = JSON.parse(configEl.textContent || "{}");
    } catch (err) {
      console.error("Invalid viewer config JSON", err);
    }
  }

  const imageUrl = config.imageUrl || "";
  const ocrUrl = config.ocrUrl || "";
  const currentFramework = config.currentFramework || "kraken";
  const editable = !!config.editable;
  const osdConfig = config.osdConfig || {};

  const frameworkEl = document.getElementById("ocr-framework");
  const statusEl = document.getElementById("ocr-status");
  const btnEl = document.getElementById("rerun-ocr-btn");
  const textarea = document.getElementById("page-text");
  const display = document.getElementById("page-text-display");

  if (frameworkEl && currentFramework) {
    frameworkEl.value = currentFramework;
  }

  if (imageUrl) {
    if (!window.OpenSeadragon) {
      console.error("OpenSeadragon is not available");
    } else {
      const defaultConfig = {
        id: "osd",
        prefixUrl: "https://cdn.jsdelivr.net/npm/openseadragon@5.0.1/build/openseadragon/images/",
        tileSources: {
          type: "image",
          url: imageUrl,
        },
        showNavigator: true,
        maxZoomPixelRatio: 2,
        visibilityRatio: 1,
        constrainDuringPan: true,
      };

      const finalConfig = { ...defaultConfig, ...osdConfig };
      finalConfig.id = "osd";
      finalConfig.tileSources = {
        type: "image",
        url: imageUrl,
      };

      window.OpenSeadragon(finalConfig);
    }
  }

  async function rerunOcr() {
    if (!ocrUrl || !btnEl || !statusEl) return;

    const framework = frameworkEl ? frameworkEl.value : currentFramework;

    statusEl.textContent = "Receiving OCR...";
    btnEl.disabled = true;

    try {
      const url = `${ocrUrl}?framework=${encodeURIComponent(framework)}`;
      const response = await fetch(url, {
        method: "GET",
        headers: {
          Accept: "application/json",
        },
      });

      if (!response.ok) {
        let detail = "OCR failed";
        try {
          const err = await response.json();
          if (err && err.detail) detail = err.detail;
        } catch (_) {}
        throw new Error(detail);
      }

      const raw = await response.text();

      let data;
      try {
        data = JSON.parse(raw);
      } catch (err) {
        console.error("RAW RESPONSE:", raw);
        throw new Error("Server returned non-JSON response");
      }

      const newText = data.text || "";

      if (editable && textarea) {
        textarea.value = newText;
      } else if (display) {
        display.textContent = newText || "[no text on this page]";
      }

      statusEl.textContent = "OCR loaded, not saved yet!";
    } catch (err) {
      statusEl.textContent = err?.message || "OCR failed";
    } finally {
      btnEl.disabled = false;
    }
  }

  if (btnEl) {
    btnEl.addEventListener("click", rerunOcr);
  }
});