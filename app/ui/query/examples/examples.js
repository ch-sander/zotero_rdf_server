document.addEventListener("DOMContentLoaded", async () => {
  const container = document.getElementById("example-queries");
  const sparnatural = document.querySelector("spar-natural");

  if (!container || !sparnatural) {
    console.error("Container oder Sparnatural-Komponente nicht gefunden.");
    return;
  }

  try {
    const response = await fetch("./examples/examples.json");

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const examples = await response.json();

    examples.forEach((example, index) => {
      const link = document.createElement("a");

      link.href = "#";
      link.textContent = example.label;

      link.addEventListener("click", event => {
        event.preventDefault();

        sparnatural.loadQuery(
          structuredClone(example.query)
        );
      });

      container.appendChild(link);

      if (index < examples.length - 1) {
        container.append(" | ");
      }
    });
  } catch (error) {
    console.error("Examples konnten nicht geladen werden:", error);
    container.textContent = "Examples konnten nicht geladen werden.";
  }
});