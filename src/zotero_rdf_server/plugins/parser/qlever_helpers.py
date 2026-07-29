import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyoxigraph import Literal, NamedNode, Store

DEFAULT_TOKEN_PATTERN = re.compile(r"\w+", flags=re.UNICODE)

ABSOLUTE_IRI_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.-]*:"
)

@dataclass(frozen=True)
class QLeverExportStats:
    records: int
    word_occurrences: int
    entity_occurrences: int
    docs_file: Path
    words_file: Path

@dataclass(frozen=True)
class _WordEntry:
    value: str
    is_entity: int
    record_id: int
    score: int
    sequence: int

def write_qlever_text_index(
    store: Store,
    config: Mapping[str, Any],
    *,
    load_text_like: Callable[[str | Path], str],
    base_dir: str | Path = ".",
    tokenizer: Callable[[str], Iterable[str]] | None = None,
) -> QLeverExportStats:
    """
    Execute two SPARQL SELECT queries against a PyOxigraph Store and write
    finished QLever wordsfile/docsfile TSV files.

    Expected docs query variables:
        ?record_id ?text

    Expected words query variables:
        ?word ?is_entity ?record_id ?score

    For is_entity = 0, ?word may contain a multiword annotation. It is
    tokenized into one QLever wordsfile row per token.

    For is_entity = 1, ?word must be a NamedNode or an absolute IRI encoded
    as a literal/string.

    The generated files contain no header.
    """
    section = config.get("qlever_text_index", config)
    root = Path(base_dir)

    docs_query_path = _required_config_value(section, "docs_query")    
    words_query_path = _required_config_value(section, "words_query")
    docs_file_path = _resolve_path(
        root, _required_config_value(section, "docs_file")
    )
    words_file_path = _resolve_path(
        root, _required_config_value(section, "words_file")
    )

    lowercase = bool(section.get("lowercase", True))
    use_union = bool(section.get("use_default_graph_as_union", False))
    require_word_and_entity = bool(
        section.get("require_word_and_entity", True)
    )

    configured_max = section.get(
        "max_record_id",
        (1 << 63) - 1,
    )
    max_record_id = (
        None
        if configured_max is None
        else int(configured_max)
    )

    if tokenizer is None:
        tokenizer = lambda text: DEFAULT_TOKEN_PATTERN.findall(text)

    docs_query = load_text_like(docs_query_path)
    words_query = load_text_like(words_query_path)

    source_docs = _read_docs_query(
        store,
        docs_query,
        use_default_graph_as_union=use_union,
        max_record_id=None,
    )

    source_to_qlever_id = {
        source_id: qlever_id
        for qlever_id, source_id in enumerate(sorted(source_docs))
    }

    docs = {
        source_to_qlever_id[source_id]: text
        for source_id, text in source_docs.items()
    }

    word_entries = _read_words_query(
        store,
        words_query,
        source_to_qlever_id=source_to_qlever_id,
        tokenizer=tokenizer,
        lowercase=lowercase,
        use_default_graph_as_union=use_union,
    )

    _validate_record_sets(
        docs=docs,
        entries=word_entries,
        require_word_and_entity=require_word_and_entity,
    )

    docs_lines = (
        f"{record_id}\t{docs[record_id]}\n"
        for record_id in sorted(docs)
    )

    sorted_entries = sorted(
        word_entries,
        key=lambda entry: (
            entry.record_id,
            entry.is_entity,
            entry.sequence,
            entry.value,
        ),
    )

    words_lines = (
        f"{entry.value}\t"
        f"{entry.is_entity}\t"
        f"{entry.record_id}\t"
        f"{entry.score}\n"
        for entry in sorted_entries
    )

    _atomic_write_text(docs_file_path, docs_lines)
    _atomic_write_text(words_file_path, words_lines)

    word_count = sum(
        entry.is_entity == 0 for entry in word_entries
    )
    entity_count = sum(
        entry.is_entity == 1 for entry in word_entries
    )

    return QLeverExportStats(
        records=len(docs),
        word_occurrences=word_count,
        entity_occurrences=entity_count,
        docs_file=docs_file_path,
        words_file=words_file_path,
    )


def _read_docs_query(
    store: Store,
    query: str,
    *,
    use_default_graph_as_union: bool,
    max_record_id: int | None,
) -> dict[int, str]:
    results = store.query(
        query,
        use_default_graph_as_union=use_default_graph_as_union,
    )
    _require_select_result(
        results,
        required_variables={"record_id", "text"},
        query_name="docs query",
    )

    docs: dict[int, str] = {}

    for row_number, solution in enumerate(results, start=1):
        record_id = _record_id_from_term(
            _required_binding(
                solution, "record_id", "docs query", row_number
            ),
            max_record_id=max_record_id,
            context=f"docs query, row {row_number}",
        )

        text_term = _required_binding(
            solution, "text", "docs query", row_number
        )
        text = _literal_lexical_value(
            text_term,
            variable="text",
            context=f"docs query, row {row_number}",
        )
        text = _normalize_docs_text(text)

        previous = docs.get(record_id)
        if previous is not None and previous != text:
            raise ValueError(
                "Conflicting docsfile texts for record_id "
                f"{record_id}: {previous!r} versus {text!r}"
            )

        docs[record_id] = text

    if not docs:
        raise ValueError("The docs query returned no text records")

    return docs


def _read_words_query(
    store: Store,
    query: str,
    *,
    source_to_qlever_id: Mapping[int, int],
    tokenizer: Callable[[str], Iterable[str]],
    lowercase: bool,
    use_default_graph_as_union: bool,
) -> list[_WordEntry]:
    results = store.query(
        query,
        use_default_graph_as_union=use_default_graph_as_union,
    )

    entries: list[_WordEntry] = []
    seen_source_rows: set[tuple[int, int, str, int]] = set()
    sequence = 0

    for row_number, solution in enumerate(results, start=1):
        context = f"words query, row {row_number}"

        source_record_id = _record_id_from_term(
            _required_binding(
                solution, "record_id", "words query", row_number
            ),
            max_record_id=None,
            context=context,
        )

        try:
            record_id = source_to_qlever_id[source_record_id]
        except KeyError as error:
            raise ValueError(
                f"{context}: source record ID {source_record_id} "
                "was not returned by the docs query"
            ) from error

        # Danach wie bisher:
        word_term = _required_binding(
            solution, "word", "words query", row_number
        )
        is_entity = _binary_integer_from_term(
            _required_binding(
                solution, "is_entity", "words query", row_number
            ),
            variable="is_entity",
            context=context,
        )
        score = _integer_from_term(
            _required_binding(
                solution, "score", "words query", row_number
            ),
            variable="score",
            context=context,
        )

        if score < 0:
            raise ValueError(
                f"{context}: score must not be negative, got {score}"
            )

        source_identity = _term_identity(word_term)
        source_key = (
            record_id,
            is_entity,
            source_identity,
            score,
        )

        if source_key in seen_source_rows:
            continue
        seen_source_rows.add(source_key)

        if is_entity == 1:
            entity = _entity_as_qlever_term(
                word_term,
                context=context,
            )
            entries.append(
                _WordEntry(
                    value=entity,
                    is_entity=1,
                    record_id=record_id,
                    score=score,
                    sequence=sequence,
                )
            )
            sequence += 1
            continue

        surface = _literal_lexical_value(
            word_term,
            variable="word",
            context=context,
        )

        tokens = list(tokenizer(surface))
        if lowercase:
            tokens = [token.lower() for token in tokens]

        tokens = [
            _validate_word_token(token, context=context)
            for token in tokens
            if token
        ]

        if not tokens:
            raise ValueError(
                f"{context}: word value {surface!r} produced no tokens"
            )

        for token in tokens:
            entries.append(
                _WordEntry(
                    value=token,
                    is_entity=0,
                    record_id=record_id,
                    score=score,
                    sequence=sequence,
                )
            )
            sequence += 1

    if not entries:
        raise ValueError("The words query returned no usable entries")

    return entries


def _validate_record_sets(
    *,
    docs: Mapping[int, str],
    entries: Iterable[_WordEntry],
    require_word_and_entity: bool,
) -> None:
    docs_ids = set(docs)
    word_ids: set[int] = set()
    entity_ids: set[int] = set()

    for entry in entries:
        if entry.is_entity:
            entity_ids.add(entry.record_id)
        else:
            word_ids.add(entry.record_id)

    referenced_ids = word_ids | entity_ids
    missing_docs = referenced_ids - docs_ids
    if missing_docs:
        preview = ", ".join(
            str(value) for value in sorted(missing_docs)[:10]
        )
        raise ValueError(
            "Words query references record IDs missing from docs query: "
            f"{preview}"
        )

    docs_without_entries = docs_ids - referenced_ids
    if docs_without_entries:
        preview = ", ".join(
            str(value) for value in sorted(docs_without_entries)[:10]
        )
        raise ValueError(
            "Docs query produced records with no wordsfile entries: "
            f"{preview}"
        )

    if require_word_and_entity:
        missing_words = docs_ids - word_ids
        missing_entities = docs_ids - entity_ids

        if missing_words:
            preview = ", ".join(
                str(value) for value in sorted(missing_words)[:10]
            )
            raise ValueError(
                "Records without a normal word posting: "
                f"{preview}"
            )

        if missing_entities:
            preview = ", ".join(
                str(value) for value in sorted(missing_entities)[:10]
            )
            raise ValueError(
                "Records without an entity posting: "
                f"{preview}"
            )


def _require_select_result(
    result: Any,
    *,
    required_variables: set[str],
    query_name: str,
) -> None:
    variables = getattr(result, "variables", None)
    if variables is None:
        raise TypeError(
            f"{query_name} must be a SPARQL SELECT query"
        )

    available = {
        variable.value for variable in variables
    }
    missing = required_variables - available

    if missing:
        raise ValueError(
            f"{query_name} is missing variables "
            f"{sorted(missing)}; available variables are "
            f"{sorted(available)}"
        )


def _required_binding(
    solution: Any,
    variable: str,
    query_name: str,
    row_number: int,
) -> Any:
    try:
        term = solution[variable]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError(
            f"{query_name}, row {row_number}: "
            f"missing binding ?{variable}"
        ) from error

    if term is None:
        raise ValueError(
            f"{query_name}, row {row_number}: "
            f"?{variable} is unbound"
        )

    return term


def _record_id_from_term(
    term: Any,
    *,
    max_record_id: int | None,
    context: str,
) -> int:
    record_id = _integer_from_term(
        term,
        variable="record_id",
        context=context,
    )

    if record_id < 0:
        raise ValueError(
            f"{context}: record_id must be nonnegative, "
            f"got {record_id}"
        )

    if (
        max_record_id is not None
        and record_id > max_record_id
    ):
        raise ValueError(
            f"{context}: record_id {record_id} exceeds "
            f"configured maximum {max_record_id}"
        )

    return record_id


def _integer_from_term(
    term: Any,
    *,
    variable: str,
    context: str,
) -> int:
    if not isinstance(term, Literal):
        raise TypeError(
            f"{context}: ?{variable} must be an RDF literal, "
            f"got {type(term).__name__}"
        )

    lexical = term.value.strip()

    if not re.fullmatch(r"[+-]?\d+", lexical):
        raise ValueError(
            f"{context}: ?{variable} must contain an integer, "
            f"got {lexical!r}"
        )

    return int(lexical)


def _binary_integer_from_term(
    term: Any,
    *,
    variable: str,
    context: str,
) -> int:
    value = _integer_from_term(
        term,
        variable=variable,
        context=context,
    )
    if value not in (0, 1):
        raise ValueError(
            f"{context}: ?{variable} must be 0 or 1, got {value}"
        )
    return value


def _literal_lexical_value(
    term: Any,
    *,
    variable: str,
    context: str,
) -> str:
    if not isinstance(term, Literal):
        raise TypeError(
            f"{context}: ?{variable} must be an RDF literal, "
            f"got {type(term).__name__}"
        )

    return term.value


def _entity_as_qlever_term(
    term: Any,
    *,
    context: str,
) -> str:
    if isinstance(term, NamedNode):
        iri = term.value
    elif isinstance(term, Literal):
        value = term.value.strip()

        if value.startswith("<") and value.endswith(">"):
            iri = value[1:-1]
        elif ABSOLUTE_IRI_PATTERN.match(value):
            iri = value
        else:
            raise ValueError(
                f"{context}: entity literal is not an absolute IRI: "
                f"{value!r}"
            )
    else:
        raise TypeError(
            f"{context}: entity must be a NamedNode or an IRI "
            f"literal, got {type(term).__name__}"
        )

    if not iri or any(character in iri for character in "\t\r\n<>"):
        raise ValueError(
            f"{context}: entity IRI cannot be written safely: {iri!r}"
        )

    return f"<{iri}>"


def _validate_word_token(
    token: str,
    *,
    context: str,
) -> str:
    if not isinstance(token, str):
        raise TypeError(
            f"{context}: tokenizer returned "
            f"{type(token).__name__}, expected str"
        )

    if not token:
        raise ValueError(f"{context}: tokenizer returned an empty token")

    if any(character in token for character in "\t\r\n"):
        raise ValueError(
            f"{context}: token contains TSV control characters: "
            f"{token!r}"
        )

    return token


def _normalize_docs_text(text: str) -> str:
    if "\x00" in text:
        raise ValueError("docsfile text contains a NUL character")

    # A QLever docsfile record must stay on one physical TSV line.
    return re.sub(r"[\t\r\n]+", " ", text).strip()


def _term_identity(term: Any) -> str:
    if isinstance(term, NamedNode):
        return f"iri:{term.value}"

    if isinstance(term, Literal):
        datatype = (
            term.datatype.value if term.datatype is not None else ""
        )
        language = term.language or ""
        return (
            f"literal:{term.value}\x1f"
            f"{datatype}\x1f{language}"
        )

    return f"{type(term).__name__}:{term!s}"


def _required_config_value(
    config: Mapping[str, Any],
    key: str,
) -> Any:
    try:
        value = config[key]
    except KeyError as error:
        raise KeyError(
            f"Missing QLever text export setting: {key}"
        ) from error

    if value is None or value == "":
        raise ValueError(
            f"QLever text export setting {key!r} is empty"
        )

    return value


def _resolve_path(root: Path, value: Any) -> Path:
    root = root.resolve()
    path = Path(value)
    if path.is_absolute():
        raise ValueError("No absolute paths!")
    target = (root / path).resolve(strict=False)
    if not target.is_relative_to(root):
        raise ValueError(f"Path not allowed: {value!r}")
    return target


def _atomic_write_text(
    target: Path,
    lines: Iterable[str],
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)

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
            encoding="utf-8",
            newline="\n",
        ) as output:
            for line in lines:
                output.write(line)

            output.flush()
            os.fsync(output.fileno())

        os.replace(temporary_path, target)

    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise