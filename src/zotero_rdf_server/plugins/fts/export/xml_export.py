import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from xml.sax.saxutils import escape, quoteattr
from zotero_rdf_server.logging_config import logger
from zotero_rdf_server.plugins.fts.export.export_paths import resolve_export_path
from zotero_rdf_server.utils import load_text_like

class RawXML(str):
    """Marker for already serialized XML."""


class XmlTemplateDict(dict[str, Any]):
    """Mapping for str.format_map with XML-safe default rendering."""

    def __missing__(self, key: str) -> str:
        return ""

    def __getitem__(self, key: str) -> str:
        value = super().get(key, "")
        if value is None:
            return ""
        if isinstance(value, RawXML):
            return str(value)
        return escape(str(value))


def make_xml_data(
    data: Mapping[str, Any],
) -> dict[str, Any]:
    """Add escaped-text, raw-XML and attribute-friendly template values."""
    result: dict[str, Any] = dict(data)

    for key, value in tuple(result.items()):
        if value is None:
            result[f"xml_{key}"] = RawXML("")
            result[f"attr_{key}"] = ""
            continue

        result[f"xml_{key}"] = RawXML(str(value))
        result[f"attr_{key}"] = quoteattr(str(value))[1:-1]

    return result


@dataclass
class XmlItemBuffer:
    """Buffered XML document for one item."""

    path: Path
    data: dict[str, Any]
    output: io.StringIO


class XmlTemplateSink:
    """Write one XML document per item from simple templates."""

    def __init__(
        self,
        output: str | Path,
        *,
        spec: Mapping[str, str],
        base_dir: str | Path = ".",
        encoding: str = "utf-8",
    ) -> None:
        self.output = output
        self.base_dir = Path(base_dir)
        self.encoding = encoding
        self.spec = {
            "item": load_text_like(spec["item"]),
            "page": load_text_like(spec["page"]),
            "footer": load_text_like(spec["footer"]),
        }
        self._current: XmlItemBuffer | None = None

    def __enter__(self) -> "XmlTemplateSink":
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
        *,
        node_value: Any = None,
    ) -> XmlItemBuffer:
        self.close_current()

        values = self._values(
            data,
            node_value=node_value,
        )
        path = resolve_export_path(
            self.output,
            base_dir=self.base_dir,
            data=values,
            allow_absolute=False,
        )
        buffer = XmlItemBuffer(
            path=path,
            data=dict(data),
            output=io.StringIO(),
        )
        self._current = buffer
        self._write(
            buffer,
            context="item",
            data=data,
            node_value=node_value,
        )
        return buffer

    def emit(
        self,
        *,
        context: str,
        data: Mapping[str, Any],
        node_value: Any = None,
    ) -> bool:
        if context == "item":
            self.begin_item(
                data,
                node_value=node_value,
            )
            return True

        buffer = self._ensure_current()
        self._write(
            buffer,
            context=context,
            data=data,
            node_value=node_value,
        )

        if context in {"footer", "end", "item_footer"}:
            self.dump_item(buffer)

        return True

    def emit_item(
        self,
        data: Mapping[str, Any],
        node_value: Any = None,
    ) -> bool:
        return self.emit(
            context="item",
            data=data,
            node_value=node_value,
        )

    def emit_page(
        self,
        data: Mapping[str, Any],
        node_value: Any = None,
    ) -> bool:
        return self.emit(
            context="page",
            data=data,
            node_value=node_value,
        )

    def emit_footer(
        self,
        data: Mapping[str, Any],
        node_value: Any = None,
    ) -> bool:
        return self.emit(
            context="footer",
            data=data,
            node_value=node_value,
        )

    def dump_item(
        self,
        buffer: XmlItemBuffer | None = None,
    ) -> None:
        buffer = buffer or self._ensure_current()
        text = buffer.output.getvalue()
        _atomic_write_text(
            buffer.path,
            text,
            encoding=self.encoding,
        )
        logger.info(
            "Dumped XML item to %s with %s bytes",
            buffer.path,
            len(text.encode(self.encoding)),
        )
        if self._current is buffer:
            self._current = None

    def close_current(self) -> None:
        self._current = None

    def _write(
        self,
        buffer: XmlItemBuffer,
        *,
        context: str,
        data: Mapping[str, Any],
        node_value: Any = None,
    ) -> None:
        try:
            template = self.spec[context]
        except KeyError as error:
            raise KeyError(
                f"Unknown XML template context {context!r}"
            ) from error

        rendered = template.format_map(
            XmlTemplateDict(
                self._values(
                    data,
                    node_value=node_value,
                )
            )
        )
        buffer.output.write(rendered)

    def _values(
        self,
        data: Mapping[str, Any],
        *,
        node_value: Any = None,
    ) -> dict[str, Any]:
        values = make_xml_data(data)
        if node_value is not None:
            values["node"] = node_value
            values["xml_node"] = RawXML(str(node_value))
            values["attr_node"] = quoteattr(str(node_value))[1:-1]
        return values

    def _ensure_current(self) -> XmlItemBuffer:
        if self._current is None:
            raise RuntimeError(
                "XmlTemplateSink has no active item"
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

        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, target)

    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
