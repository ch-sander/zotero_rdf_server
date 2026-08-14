from pathlib import Path
from typing import Any, Mapping


class SafeFormatDict(dict[str, Any]):
    """Format mapping that leaves unknown path placeholders empty."""

    def __missing__(self, key: str) -> str:
        return ""

    def __getitem__(self, key: str) -> str:
        value = super().get(key, "")
        if value is None:
            return ""
        return str(value)


def render_path_template(
    value: str | Path,
    data: Mapping[str, Any] | None = None,
) -> str:
    """Render a path template with export data."""
    return str(value).format_map(
        SafeFormatDict(dict(data or {}))
    )


def resolve_export_path(
    value: str | Path,
    *,
    base_dir: str | Path = ".",
    data: Mapping[str, Any] | None = None,
    allow_absolute: bool = False,
) -> Path:
    """Render and resolve an export path below base_dir."""
    rendered = render_path_template(
        value,
        data=data,
    )
    root = Path(base_dir).resolve(strict=False)
    path = Path(rendered)

    if path.is_absolute():
        if not allow_absolute:
            raise ValueError(
                f"Absolute export paths are not allowed: {rendered!r}"
            )
        target = path.resolve(strict=False)
    else:
        target = (root / path).resolve(strict=False)

    if not allow_absolute and not target.is_relative_to(root):
        raise ValueError(
            f"Export path escapes base directory: {rendered!r}"
        )

    return target
