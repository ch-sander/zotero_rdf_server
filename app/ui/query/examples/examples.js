document.addEventListener("DOMContentLoaded", async () => {
  const container = document.getElementById("example-queries");

  const descriptionElement = document.getElementById(
    "example-query-description"
  );

  const sparnatural = document.querySelector("spar-natural");

  const modalElement = document.getElementById(
    "exampleConfirmModal"
  );

  const modalMessage = document.getElementById(
    "example-confirm-message"
  );

  const acceptButton = document.getElementById(
    "accept-example-query"
  );

  if (
    !container ||
    !descriptionElement ||
    !sparnatural ||
    !modalElement ||
    !modalMessage ||
    !acceptButton
  ) {
    console.error(
      "Required UI elements or Sparnatural component not found."
    );

    return;
  }

  const confirmationModal = new bootstrap.Modal(
    modalElement
  );

  const showDescription = example => {
    descriptionElement.textContent =
      example.description ?? "";
  };

  try {
    const response = await fetch(
      "./examples/examples.json"
    );

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const examples = await response.json();

    /*
     * Only examples without hidden: true are displayed
     * in the HTML selection.
     */
    const visibleExamples = examples.filter(
      example => !example.hidden
    );

    visibleExamples.forEach((example, index) => {
      const link = document.createElement("a");

      link.href = "#";
      link.textContent = example.label;

      /*
       * Native browser tooltip.
       */
      link.title = example.description ?? "";

      /*
       * Associates the link with the visible description
       * for assistive technologies.
       */
      link.setAttribute(
        "aria-describedby",
        "example-query-description"
      );

      /*
       * Update the visible description on hover.
       */
      // link.addEventListener("mouseenter", () => {
      //   showDescription(example);
      // });

      /*
       * Also update it when navigating by keyboard.
       */
      // link.addEventListener("focus", () => {
      //   showDescription(example);
      // });

      link.addEventListener("click", event => {
        event.preventDefault();

        showDescription(example);

        sparnatural.loadQuery(
          structuredClone(example.query)
        );
      });

      container.appendChild(link);

      if (index < visibleExamples.length - 1) {
        container.append(" | ");
      }
    });

    /*
     * Show the description of the first visible example
     * before the user hovers over a link.
     */
    // if (visibleExamples.length > 0) {
    //   showDescription(visibleExamples[0]);
    // }

    const exampleId = new URLSearchParams(
      window.location.search
    ).get("example");

    if (!exampleId) {
      return;
    }

    /*
     * Search the complete array here, not visibleExamples.
     *
     * This means that examples with hidden: true can still
     * be opened through:
     *
     * ?example=example-id
     */
    const initialExample = examples.find(
      example => example.id === exampleId
    );

    if (!initialExample) {
      console.warn(
        `Unknown example ID: ${exampleId}`
      );

      return;
    }

    showDescription(initialExample);

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
    console.error(
      "Example queries could not be loaded:",
      error
    );

    container.textContent =
      "Example queries could not be loaded.";

    descriptionElement.textContent = "";
  }
});