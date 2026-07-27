document.addEventListener("DOMContentLoaded", async () => {
  const container = document.getElementById("example-queries");
  const sparnatural = document.querySelector("spar-natural");

  const modalElement = document.getElementById("exampleConfirmModal");
  const modalMessage = document.getElementById("example-confirm-message");
  const acceptButton = document.getElementById("accept-example-query");

  if (
    !container ||
    !sparnatural ||
    !modalElement ||
    !modalMessage ||
    !acceptButton
  ) {
    console.error("Required UI elements or Sparnatural component not found.");
    return;
  }

  const confirmationModal = new bootstrap.Modal(modalElement);

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
        sparnatural.loadQuery(structuredClone(example.query));
      });

      container.appendChild(link);

      if (index < examples.length - 1) {
        container.append(" | ");
      }
    });

    const exampleId =
      new URLSearchParams(window.location.search).get("example");

    if (!exampleId) {
      return;
    }

    const initialExample = examples.find(
      example => example.id === exampleId
    );

    if (!initialExample) {
      console.warn(`Unknown example ID: ${exampleId}`);
      return;
    }

    modalMessage.textContent =
      `Do you want to load the example query "${initialExample.label}"?`;

    acceptButton.onclick = () => {
      sparnatural.loadQuery(
        structuredClone(initialExample.query)
      );

      confirmationModal.hide();
    };

    confirmationModal.show();
  } catch (error) {
    console.error("Example queries could not be loaded:", error);
    container.textContent = "Example queries could not be loaded.";
  }
});