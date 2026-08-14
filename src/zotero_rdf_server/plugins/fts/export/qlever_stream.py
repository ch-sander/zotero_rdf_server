import gzip
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TextIO

from pyoxigraph import NamedNode


DEFAULT_TOKEN_PATTERN = re.compile(r"\w+", flags=re.UNICODE)
ABSOLUTE_IRI_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def next_record_id_from_docs(
    path: str | Path,
) -> int:
    """Return the next free QLever record_id from a docs.tsv(.gz) file."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return 0

    opener = gzip.open if path.suffix == ".gz" else open
    last_record_id = -1

    with opener(
        path,
        mode="rt",
        encoding="utf-8",
        newline="",
    ) as input_file:
        for line_number, line in enumerate(input_file, start=1):
            line = line.rstrip("\n")
            if not line:
                continue

            try:
                record_id = int(line.split("\t", 1)[0])
            except Exception as error:
                raise ValueError(
                    f"{path}: invalid docsfile line {line_number}"
                ) from error

            last_record_id = record_id

    return last_record_id + 1


class QLeverTextGzipSink:
    """Append QLever docsfile and wordsfile TSV streams."""

    def __init__(
        self,
        docs_path: str | Path,
        words_path: str | Path,
        *,
        tokenizer: Callable[[str], Iterable[str]] | None = None,
        lowercase: bool = True,
        compresslevel: int = 6,
        append: bool = False,
    ) -> None:
        self.docs_path = Path(docs_path)
        self.words_path = Path(words_path)
        self.tokenizer = tokenizer or DEFAULT_TOKEN_PATTERN.findall
        self.lowercase = lowercase
        self.compresslevel = compresslevel
        self.append = append

        self._docs_output: TextIO | None = None
        self._words_output: TextIO | None = None
        self._next_record_id = 0

    def __enter__(self) -> "QLeverTextGzipSink":
        self.docs_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.words_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._next_record_id = (
            next_record_id_from_docs(self.docs_path)
            if self.append
            else 0
        )

        mode = "at" if self.append else "wt"
        self._docs_output = gzip.open(
            self.docs_path,
            mode=mode,
            encoding="utf-8",
            newline="\n",
            compresslevel=self.compresslevel,
        )
        self._words_output = gzip.open(
            self.words_path,
            mode=mode,
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
        score: int = 1,
    ) -> int:
        docs_output, words_output = self._ensure_open()
        record_id = self._next_record_id
        self._next_record_id += 1

        normalized_text = self._normalize_text(text)
        score = self._validate_score(score)

        docs_output.write(
            f"{record_id}\t{normalized_text}\n"
        )

        source_words = self.tokenizer(text) if words is None else words
        for word in source_words:
            token = self._normalize_word(word)
            if token:
                words_output.write(
                    f"{token}\t0\t{record_id}\t{score}\n"
                )

        for entity in entities:
            entity_value = self._normalize_entity(entity)
            words_output.write(
                f"{entity_value}\t1\t{record_id}\t{score}\n"
            )

        return record_id

    @staticmethod
    def _normalize_text(text: str) -> str:
        if not isinstance(text, str):
            raise TypeError(
                f"text must be str, got {type(text).__name__}"
            )
        if "\x00" in text:
            raise ValueError("QLever document text contains NUL")
        return re.sub(r"[\t\r\n]+", " ", text).strip()

    def _normalize_word(self, word: str) -> str:
        if not isinstance(word, str):
            raise TypeError(
                f"word must be str, got {type(word).__name__}"
            )
        if self.lowercase:
            word = word.lower()
        if any(character in word for character in "\t\r\n"):
            raise ValueError(
                f"QLever word contains TSV control characters: {word!r}"
            )
        return word

    @staticmethod
    def _normalize_entity(entity: str | NamedNode) -> str:
        if isinstance(entity, NamedNode):
            iri = entity.value
        else:
            iri = str(entity).strip()
            if iri.startswith("<") and iri.endswith(">"):
                iri = iri[1:-1]

        if not ABSOLUTE_IRI_PATTERN.match(iri):
            raise ValueError(
                f"Entity is not an absolute IRI: {iri!r}"
            )
        if any(character in iri for character in "\t\r\n<>"):
            raise ValueError(
                f"Entity IRI cannot be serialized: {iri!r}"
            )
        return f"<{iri}>"

    @staticmethod
    def _validate_score(score: int) -> int:
        score = int(score)
        if score < 0:
            raise ValueError("score must be nonnegative")
        return score

    def _ensure_open(self) -> tuple[TextIO, TextIO]:
        if self._docs_output is None or self._words_output is None:
            raise RuntimeError(
                "QLeverTextGzipSink must be used as a context manager"
            )
        return self._docs_output, self._words_output
