import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from jinja2 import Environment, StrictUndefined, select_autoescape
from zotero_rdf_server.logging_config import logger
from zotero_rdf_server.plugins.fts.export.export_paths import (
    resolve_export_path,
)
from zotero_rdf_server.utils import load_text_like
from ..helpers import safe_doc_id

@dataclass
class HtmlItemBuffer:
    path: Path
    data: dict[str, Any]
    pages: list[dict[str, Any]] = field(
        default_factory=list,
    )


class HtmlJinjaSink:
    """Render one HTML document per item with Jinja2."""

    def __init__(
        self,
        output: str | Path,
        *,
        template: str | Path,
        base_dir: str | Path = ".",
        encoding: str = "utf-8",
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.output = output
        self.base_dir = Path(base_dir)
        self.encoding = encoding
        self.context = dict(context or {})

        template_source = load_text_like(template)

        self.environment = Environment(
            autoescape=select_autoescape(
                enabled_extensions=("html", "htm"),
                default_for_string=True,
            ),
            undefined=StrictUndefined,
        )

        self.template = self.environment.from_string(
            template_source
        )

        self._current: HtmlItemBuffer | None = None

    def __enter__(self) -> "HtmlJinjaSink":
        return self

    def __exit__(
        self,
        exc_type,
        exc,
        tb,
    ) -> None:
        self.close_current()

    def begin_item(
        self,
        data: Mapping[str, Any],
    ) -> HtmlItemBuffer:
        self.close_current()
        path_data = dict(data)
        path_data["_id"] = safe_doc_id(str(path_data["_id"]))
        path = resolve_export_path(
            self.output,
            base_dir=self.base_dir,
            data=path_data,
            allow_absolute=False,
        )

        buffer = HtmlItemBuffer(
            path=path,
            data=dict(data),
        )

        self._current = buffer
        return buffer

    def emit_item(
        self,
        data: Mapping[str, Any],
        node_value: Any = None,
    ) -> bool:
        self.begin_item(data)
        return True

    def emit_page(
        self,
        data: Mapping[str, Any],
        node_value: Any = None,
    ) -> bool:
        buffer = self._ensure_current()

        buffer.pages.append(
            dict(data)
        )

        return True

    def emit_footer(
        self,
        data: Mapping[str, Any] | None = None,
        node_value: Any = None,
    ) -> bool:
        self.dump_item()
        return True

    def dump_item(
        self,
        buffer: HtmlItemBuffer | None = None,
    ) -> None:
        buffer = buffer or self._ensure_current()

        render_data = {
            **self.context,
            **buffer.data,
            "data": buffer.data,
            "pages": buffer.pages,
            "page_count": len(buffer.pages),
        }

        rendered = self.template.render(
            **render_data
        )

        _atomic_write_text(
            buffer.path,
            rendered,
            encoding=self.encoding,
        )
        logger.info(
            "Dumped HTML item to %s with %s bytes",
            buffer.path,
            len(rendered.encode(self.encoding)),
        )
        if self._current is buffer:
            self._current = None

    def close_current(self) -> None:
        self._current = None

    def _ensure_current(self) -> HtmlItemBuffer:
        if self._current is None:
            raise RuntimeError(
                "HtmlJinjaSink has no active item"
            )

        return self._current


def _atomic_write_text(
    target: Path,
    text: str,
    *,
    encoding: str,
) -> None:
    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(
            file_descriptor,
            "w",
            encoding=encoding,
            newline="\n",
        ) as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())

        os.chmod(
            temporary_path,
            0o644,
        )
        os.replace(
            temporary_path,
            target,
        )

    except BaseException:
        temporary_path.unlink(
            missing_ok=True,
        )
        raise