function initReportProblemLink(attempt = 0) {
  const link = document
    .getElementById("custom-footer-container")
    ?.shadowRoot
    ?.getElementById("report-problem");

  if (!link) {
    if (attempt < 100) {
      requestAnimationFrame(() => initReportProblemLink(attempt + 1));
    }
    return;
  }

  const issueUrl = new URL(
    "https://github.com/ch-sander/scigma/issues/new"
  );

  issueUrl.searchParams.set("template", "report-a-problem.yml");
  issueUrl.searchParams.set(
    "url",
    window.location.href
  );
    issueUrl.searchParams.set(
    "title",
    document.title.trim() || "Feedback"
    );

  link.href = issueUrl.toString();
}

initReportProblemLink();