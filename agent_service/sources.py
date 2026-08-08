from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from pathlib import Path

from pydantic import BaseModel, ConfigDict


ALLOWED_SOURCE_SUFFIXES = frozenset({".md", ".txt"})
MAX_SOURCE_BYTES = 2 * 1024 * 1024
INTERNAL_TRANSFER_HEADING = "内部转账"
MARKDOWN_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
ALLOWED_PROMPTS = frozenset(
    {
        "extract_requirements",
        "analyze_risks",
        "generate_test_plan",
        "classify_failure",
    }
)


class SourceLoadError(ValueError):
    """Raised when a source cannot be loaded safely."""


class LoadedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    path: str
    version: str
    content: str


class LoadedSources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    documents: tuple[LoadedDocument, ...]

    @property
    def combined_text(self) -> str:
        return "\n\n".join(
            f"## SOURCE {document.source_id}\n{document.content}"
            for document in self.documents
        )

    @property
    def internal_transfer_text(self) -> str:
        return "\n\n".join(
            f"## SOURCE {document.source_id}\n"
            f"{_internal_transfer_excerpt(document.content)}"
            for document in self.documents
        )


def _internal_transfer_excerpt(content: str) -> str:
    lines = content.splitlines()
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = MARKDOWN_HEADING_PATTERN.match(line)
        if match is not None:
            headings.append((index, len(match.group(1)), match.group(2)))

    ranges: list[tuple[int, int]] = []
    for position, (start, level, title) in enumerate(headings):
        if INTERNAL_TRANSFER_HEADING not in title:
            continue
        end = len(lines)
        for next_start, next_level, _ in headings[position + 1 :]:
            if next_level <= level:
                end = next_start
                break
        ranges.append((start, end))

    if not ranges:
        return content

    selected: list[str] = []
    covered_until = -1
    for start, end in ranges:
        if start < covered_until:
            continue
        selected.extend(lines[start:end])
        covered_until = end
    return "\n".join(selected).strip()


def _resolve_source_path(input_path: Path) -> Path:
    resolved: Path | None = None
    try:
        resolved = Path(input_path).expanduser().resolve()
    except OSError:
        pass

    if resolved is None:
        raise SourceLoadError("unable to resolve source file safely")
    return resolved


def _read_regular_file(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)

    file_descriptor: int | None = None
    metadata: os.stat_result | None = None
    raw_content: bytes | None = None
    io_failed = False
    open_errno: int | None = None

    try:
        file_descriptor = os.open(path, flags)
    except OSError as error:
        open_errno = error.errno
        io_failed = True

    if io_failed or file_descriptor is None:
        if open_errno in {errno.ENOENT, errno.ENOTDIR}:
            raise ValueError(
                "source path must be an existing regular file"
            )
        raise SourceLoadError("unable to open source file safely")

    try:
        try:
            metadata = os.fstat(file_descriptor)
            if stat.S_ISREG(metadata.st_mode):
                chunks: list[bytes] = []
                remaining_bytes = MAX_SOURCE_BYTES + 1
                while remaining_bytes > 0:
                    chunk = os.read(file_descriptor, remaining_bytes)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining_bytes -= len(chunk)
                raw_content = b"".join(chunks)
        except OSError:
            io_failed = True
    finally:
        try:
            os.close(file_descriptor)
        except OSError:
            io_failed = True

    if io_failed:
        raise SourceLoadError("unable to read source file safely")
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("source path must be an existing regular file")
    if raw_content is None:
        raise SourceLoadError("unable to read source file safely")
    return raw_content, metadata


def _source_id(path: Path) -> str:
    normalized_path = str(path).encode("utf-8")
    digest = hashlib.sha256(normalized_path).hexdigest()[:12].upper()
    return f"SRC-{digest}"


def load_sources(paths: list[Path]) -> LoadedSources:
    documents: list[LoadedDocument] = []
    resolved_paths: set[Path] = set()
    file_identities: set[tuple[int, int]] = set()

    for input_path in paths:
        resolved = _resolve_source_path(input_path)
        if resolved.suffix.lower() not in ALLOWED_SOURCE_SUFFIXES:
            raise ValueError(f"unsupported source type: {resolved.suffix}")
        if resolved in resolved_paths:
            raise ValueError(f"duplicate source path: {resolved}")
        resolved_paths.add(resolved)

        raw_content, metadata = _read_regular_file(resolved)
        file_identity = (metadata.st_dev, metadata.st_ino)
        if file_identity in file_identities:
            raise ValueError("duplicate source file")
        file_identities.add(file_identity)

        if len(raw_content) > MAX_SOURCE_BYTES:
            raise ValueError(f"source file exceeds 2 MiB: {resolved}")
        content = raw_content.decode("utf-8")

        documents.append(
            LoadedDocument(
                source_id=_source_id(resolved),
                path=str(resolved),
                version=hashlib.sha256(raw_content).hexdigest(),
                content=content,
            )
        )

    return LoadedSources(documents=tuple(documents))


def read_prompt(name: str) -> str:
    if not isinstance(name, str) or name not in ALLOWED_PROMPTS:
        raise ValueError("unknown prompt")

    raw_content, _ = _read_regular_file(PROMPTS_DIR / f"{name}.md")
    if len(raw_content) > MAX_SOURCE_BYTES:
        raise ValueError("prompt file exceeds 2 MiB")
    return raw_content.decode("utf-8")
