import gzip
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TextIO

from pyoxigraph import NamedNode


DEFAULT_TOKEN_PATTERN = re.compile(
    r"\w+",
    flags=re.UNICODE,
)

ABSOLUTE_IRI_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.-]*:"
)


class QLeverTextGzipSink:
    """Write QLever docsfile and wordsfile gzip streams."""

    def __init__(
        self,
        docs_path: str | Path,
        words_path: str | Path,
        *,
        tokenizer: Callable[
            [str],
            Iterable[str],
        ] | None = None,
        lowercase: bool = True,
        compresslevel: int = 6,
    ) -> None:
        self.docs_path = Path(docs_path)
        self.words_path = Path(words_path)

        self.tokenizer = (
            tokenizer
            if tokenizer is not None
            else DEFAULT_TOKEN_PATTERN.findall
        )
        self.lowercase = lowercase
        self.compresslevel = compresslevel

        self._docs_output: TextIO | None = None
        self._words_output: TextIO | None = None

        self._next_record_id = 0
        self._record_ids: set[int] = set()

    def __enter__(self) -> "QLeverTextGzipSink":
        self.docs_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.words_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._docs_output = gzip.open(
            self.docs_path,
            mode="wt",
            encoding="utf-8",
            newline="\n",
            compresslevel=self.compresslevel,
        )
        self._words_output = gzip.open(
            self.words_path,
            mode="wt",
            encoding="utf-8",
            newline="\n",
            compresslevel=self.compresslevel,
        )

        return self

    def __exit__(
        self,
        exc_type,
        exc,
        tb,
    ) -> None:
        if self._words_output is not None:
            self._words_output.close()
            self._words_output = None

        if self._docs_output is not None:
            self._docs_output.close()
            self._docs_output = None

    def emit(
        self,
        *,
        text: str,
        entities: Iterable[str | NamedNode] = (),
        words: Iterable[str] | None = None,
        record_id: int | None = None,
        score: int = 1,
    ) -> int:
        """
        Write one complete QLever text record.

        When words is omitted, tokens are derived from text.
        Returns the numeric QLever record ID.
        """
        docs_output, words_output = self._ensure_open()

        record_id = self._allocate_record_id(
            record_id
        )
        normalized_text = self._normalize_text(text)
        score = self._validate_score(score)

        source_words = (
            self.tokenizer(text)
            if words is None
            else words
        )

        word_values = [
            self._normalize_word(word)
            for word in source_words
            if word
        ]

        entity_values = [
            self._normalize_entity(entity)
            for entity in entities
        ]

        # Erst vollständig validieren, danach schreiben.
        docs_output.write(
            f"{record_id}\t{normalized_text}\n"
        )

        for word in word_values:
            words_output.write(
                f"{word}\t0\t{record_id}\t{score}\n"
            )

        for entity in entity_values:
            words_output.write(
                f"{entity}\t1\t{record_id}\t{score}\n"
            )

        return record_id

    def _allocate_record_id(
        self,
        requested: int | None,
    ) -> int:
        if requested is None:
            record_id = self._next_record_id

            while record_id in self._record_ids:
                record_id += 1
        else:
            record_id = int(requested)

        if record_id < 0:
            raise ValueError(
                "record_id must be nonnegative"
            )

        if record_id in self._record_ids:
            raise ValueError(
                f"Duplicate record_id: {record_id}"
            )

        self._record_ids.add(record_id)
        self._next_record_id = max(
            self._next_record_id,
            record_id + 1,
        )

        return record_id

    def _normalize_word(
        self,
        word: str,
    ) -> str:
        if not isinstance(word, str):
            raise TypeError(
                "QLever word must be str, got "
                f"{type(word).__name__}"
            )

        if self.lowercase:
            word = word.lower()

        if not word:
            raise ValueError(
                "QLever word must not be empty"
            )

        if any(
            character in word
            for character in "\t\r\n"
        ):
            raise ValueError(
                "QLever word contains TSV control "
                f"characters: {word!r}"
            )

        return word

    @staticmethod
    def _normalize_entity(
        entity: str | NamedNode,
    ) -> str:
        if isinstance(entity, NamedNode):
            iri = entity.value
        else:
            iri = str(entity).strip()

            if (
                iri.startswith("<")
                and iri.endswith(">")
            ):
                iri = iri[1:-1]

        if not ABSOLUTE_IRI_PATTERN.match(iri):
            raise ValueError(
                f"Entity is not an absolute IRI: {iri!r}"
            )

        if any(
            character in iri
            for character in "\t\r\n<>"
        ):
            raise ValueError(
                f"Entity IRI cannot be serialized: {iri!r}"
            )

        return f"<{iri}>"

    @staticmethod
    def _normalize_text(text: str) -> str:
        if not isinstance(text, str):
            raise TypeError(
                "QLever document text must be str, got "
                f"{type(text).__name__}"
            )

        if "\x00" in text:
            raise ValueError(
                "QLever document text contains NUL"
            )

        return re.sub(
            r"[\t\r\n]+",
            " ",
            text,
        ).strip()

    @staticmethod
    def _validate_score(score: int) -> int:
        score = int(score)

        if score < 0:
            raise ValueError(
                "score must be nonnegative"
            )

        return score

    def _ensure_open(
        self,
    ) -> tuple[TextIO, TextIO]:
        if (
            self._docs_output is None
            or self._words_output is None
        ):
            raise RuntimeError(
                "QLeverTextGzipSink must be used "
                "as a context manager"
            )

        return (
            self._docs_output,
            self._words_output,
        )