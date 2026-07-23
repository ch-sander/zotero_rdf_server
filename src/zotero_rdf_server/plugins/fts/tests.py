from dataclasses import asdict, dataclass
from time import perf_counter
from typing import Mapping, Optional
import json
import requests
from .helpers import Kind

@dataclass(frozen=True)
class UrlKindTestCase:
    url: str
    expected: Kind


@dataclass(frozen=True)
class UrlKindTestResult:
    name: str
    url: str
    expected: Kind
    detected: Optional[Kind]
    passed: bool
    duration_ms: float
    error: Optional[str] = None


DEFAULT_URL_KIND_TESTS: dict[str, UrlKindTestCase] = {
    "iiif_v3": UrlKindTestCase(
        url="https://iiif.io/api/cookbook/recipe/0001-mvm-image/manifest.json",
        expected="iiif",
    ),
    "iiif_v2_gallica": UrlKindTestCase(
        url="https://gallica.bnf.fr/iiif/ark:/12148/bd6t57376380/manifest.json",
        expected="iiif",
    ),
    "iiif_heidelberg": UrlKindTestCase(
        url="https://digi.ub.uni-heidelberg.de/diglit/iiif3/wild1677/manifest",
        expected="iiif",
    ),
    "iiif_halle": UrlKindTestCase(
        url="https://opendata2.uni-halle.de/explore?bitstream_id=435ff39f-9f37-495f-b028-f0a9eecdbe34&handle=1516514412012/6307&provider=iiif-image&onlyManifest=true",
        expected="iiif",
    ),
    "iiif_slub": UrlKindTestCase(
        url="https://iiif.slub-dresden.de/iiif/2/331561352/manifest.json",
        expected="iiif",
    ),
    "iiif_mdz": UrlKindTestCase(
        url="https://digi.ub.uni-heidelberg.de/diglit/iiif3/wild1677/manifest",
        expected="iiif",
    ),
    "iiif_sbb": UrlKindTestCase(
        url="https://content.staatsbibliothek-berlin.de/dc/669150924/manifest",
        expected="iiif",
    ), 
    "json_object": UrlKindTestCase(
        url="https://jsonplaceholder.typicode.com/todos/1",
        expected="json",
    ),
    "json_array": UrlKindTestCase(
        url="https://jsonplaceholder.typicode.com/posts",
        expected="json",
    ),
    "xml": UrlKindTestCase(
        url="https://www.w3schools.com/xml/note.xml",
        expected="xml",
    ),
    "html": UrlKindTestCase(
        url="https://example.com/",
        expected="html",
    ),
    "text": UrlKindTestCase(
        url="https://www.gutenberg.org/files/11/11-0.txt",
        expected="text",
    ),
    "csv": UrlKindTestCase(
        url=(
            "https://www2.census.gov/programs-surveys/popest/datasets/"
            "2010-2019/national/totals/"
            "nst-est2019-popchg2010_2019.csv"
        ),
        expected="csv",
    ),
    "pdf": UrlKindTestCase(
        url=(
            "https://www.w3.org/WAI/ER/tests/xhtml/"
            "testfiles/resources/pdf/dummy.pdf"
        ),
        expected="pdf",
    ),
}


def test_url_kind_detection(
    tests: Optional[Mapping[str, UrlKindTestCase]] = None,
    *,
    timeout: int = 30,
    sniff_bytes: int = 16_384,
    strict: bool = False,
    verbose: bool = True,
    session: Optional[requests.Session] = None,
) -> list[UrlKindTestResult]:
    """
    Test detect_url_kind() against a set of known URLs.

    Args:
        tests:
            Mapping of test names to UrlKindTestCase instances.
            Uses DEFAULT_URL_KIND_TESTS when omitted.

        timeout:
            Request timeout passed to detect_url_kind().

        sniff_bytes:
            Number of bytes used for format detection.

        strict:
            Raise AssertionError after the test run if any test fails.

        verbose:
            Print a formatted result table.

        session:
            Optional shared requests.Session.

    Returns:
        A list of UrlKindTestResult instances.
    """
    selected_tests = tests or DEFAULT_URL_KIND_TESTS
    owns_session = session is None
    http = session or requests.Session()

    results: list[UrlKindTestResult] = []

    try:
        for name, case in selected_tests.items():
            url = case.url.strip().rstrip("\u2060\u200b\ufeff")
            started = perf_counter()

            try:
                from .helpers import detect_url_kind
                from .ocr import APP_USER
                detected = detect_url_kind(
                    url,
                    timeout=timeout,
                    sniff_bytes=sniff_bytes,
                    session=http,
                    request_headers=APP_USER
                )

                duration_ms = (perf_counter() - started) * 1000

                result = UrlKindTestResult(
                    name=name,
                    url=url,
                    expected=case.expected,
                    detected=detected,
                    passed=detected == case.expected,
                    duration_ms=duration_ms,
                )

            except Exception as exc:
                duration_ms = (perf_counter() - started) * 1000

                result = UrlKindTestResult(
                    name=name,
                    url=url,
                    expected=case.expected,
                    detected=None,
                    passed=False,
                    duration_ms=duration_ms,
                    error=f"{type(exc).__name__}: {exc}",
                )

            results.append(result)

    finally:
        if owns_session:
            http.close()

    if verbose:
        _print_url_kind_test_results(results)

    failures = [result for result in results if not result.passed]

    if strict and failures:
        failure_details = "; ".join(
            (
                f"{result.name}: expected={result.expected!r}, "
                f"detected={result.detected!r}"
                + (f", error={result.error}" if result.error else "")
            )
            for result in failures
        )

        raise AssertionError(
            f"{len(failures)} URL kind test(s) failed: {failure_details}"
        )

    return results


def _print_url_kind_test_results(
    results: list[UrlKindTestResult],
) -> None:
    name_width = max(
        len("Test"),
        *(len(result.name) for result in results),
    )

    print(
        f"{'Status':<7} "
        f"{'Test':<{name_width}} "
        f"{'Expected':<9} "
        f"{'Detected':<9} "
        f"{'Duration':>10}"
    )
    print("-" * (39 + name_width))

    for result in results:
        status = "OK" if result.passed else "FAIL"
        detected = result.detected or "-"
        duration = f"{result.duration_ms:.0f} ms"

        print(
            f"{status:<7} "
            f"{result.name:<{name_width}} "
            f"{result.expected:<9} "
            f"{detected:<9} "
            f"{duration:>10}"
        )

        if result.error:
            print(f"        Error: {result.error}")

    passed_count = sum(result.passed for result in results)

    print(
        f"\n{passed_count}/{len(results)} tests passed."
    )