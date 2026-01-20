from typing import Iterator, Tuple
from dataclasses import dataclass

@dataclass(frozen=True)
class PdfTextPolicy:
    enabled: bool = True
    min_chars: int = 80
    min_alpha_ratio: float = 0.6

def url_to_text_pages(
    url: str,
    *,
    config_path: str | None = None,
    domain: str | None = None,
    model_name: str | None = None,
    segmenter: str | None = None,
    iiif_max_width: int = 2000,
    pdf_dpi: int = 200,
    pdf_text_policy: PdfTextPolicy = PdfTextPolicy(),
    binarize: bool = True,
) -> Iterator[Tuple[int, str]]:
    from .ocr import iter_pages, page_to_text

    for item in iter_pages(
        url,
        iiif_max_width=iiif_max_width,
        pdf_dpi=pdf_dpi,
        pdf_text_policy=pdf_text_policy,
    ):
        yield item.index, page_to_text(
            item,
            config_path=config_path,
            domain=domain,
            model_name=model_name,
            segmenter=segmenter,
            binarize=binarize,
        )
