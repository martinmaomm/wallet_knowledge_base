from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from agent_service import sources as source_module
from agent_service.sources import MAX_SOURCE_BYTES, SourceLoadError, load_sources


FIXTURE = Path("tests/agent/fixtures/web2_internal_transfer.md")


def expected_source_id(path: Path) -> str:
    normalized_path = str(path.expanduser().resolve()).encode("utf-8")
    digest = hashlib.sha256(normalized_path).hexdigest()[:12].upper()
    return f"SRC-{digest}"


def test_source_loader_preserves_input_order_and_hashes_original_utf8() -> None:
    first_bytes = FIXTURE.read_bytes()
    loaded = load_sources([FIXTURE, Path("README.md")])

    assert [item.source_id for item in loaded.documents] == [
        expected_source_id(FIXTURE),
        expected_source_id(Path("README.md")),
    ]
    assert loaded.documents[0].path == str(FIXTURE.resolve())
    assert loaded.documents[0].version == hashlib.sha256(first_bytes).hexdigest()
    assert loaded.documents[0].content == first_bytes.decode("utf-8")
    assert loaded.combined_text.startswith(
        f"## SOURCE {expected_source_id(FIXTURE)}\n# Web2 内部转账"
    )
    assert f"## SOURCE {expected_source_id(Path('README.md'))}\n" in (
        loaded.combined_text
    )


def test_source_ids_are_stable_when_input_order_changes() -> None:
    paths = [FIXTURE, Path("README.md")]

    forward = load_sources(paths)
    reversed_order = load_sources(list(reversed(paths)))

    forward_ids = {
        document.path: document.source_id
        for document in forward.documents
    }
    reversed_ids = {
        document.path: document.source_id
        for document in reversed_order.documents
    }
    assert forward_ids == reversed_ids


def test_source_loader_hashes_crlf_bytes_without_newline_normalization(
    tmp_path: Path,
) -> None:
    path = tmp_path / "source.txt"
    raw_content = b"first\r\nsecond\r\n"
    path.write_bytes(raw_content)

    loaded = load_sources([path])

    assert loaded.documents[0].content == "first\r\nsecond\r\n"
    assert loaded.documents[0].version == hashlib.sha256(raw_content).hexdigest()


def test_source_loader_allows_an_empty_path_list() -> None:
    loaded = load_sources([])

    assert loaded.documents == ()
    assert loaded.combined_text == ""


def test_loaded_documents_are_immutable_as_a_collection() -> None:
    loaded = load_sources([FIXTURE])

    with pytest.raises(AttributeError):
        loaded.documents.append(loaded.documents[0])


@pytest.mark.parametrize(
    ("kind", "expected_message"),
    [
        ("missing", "existing regular file"),
        ("directory", "existing regular file"),
        ("extension", "unsupported source type"),
    ],
)
def test_source_loader_rejects_invalid_source_paths(
    tmp_path: Path,
    kind: str,
    expected_message: str,
) -> None:
    if kind == "missing":
        path = tmp_path / "missing.md"
    elif kind == "directory":
        path = tmp_path / "folder.md"
        path.mkdir()
    else:
        path = tmp_path / "source.docx"
        path.write_bytes(b"content")

    with pytest.raises(ValueError, match=expected_message):
        load_sources([path])


def test_source_loader_rejects_duplicate_resolved_paths(tmp_path: Path) -> None:
    path = tmp_path / "source.md"
    path.write_text("content", encoding="utf-8")
    alias = tmp_path / "alias.md"
    alias.symlink_to(path)

    with pytest.raises(ValueError, match="duplicate source path"):
        load_sources([path, alias])


def test_source_loader_rejects_hard_link_aliases(tmp_path: Path) -> None:
    path = tmp_path / "source.md"
    path.write_text("content", encoding="utf-8")
    alias = tmp_path / "hard-link.md"
    os.link(path, alias)

    with pytest.raises(ValueError, match="duplicate source"):
        load_sources([path, alias])


def test_source_loader_rejects_files_larger_than_two_mib(
    tmp_path: Path,
) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"a" * (MAX_SOURCE_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds 2 MiB"):
        load_sources([path])


def test_source_loader_reads_at_most_maximum_plus_one_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "growing.txt"
    path.write_bytes(b"a" * (MAX_SOURCE_BYTES + 100))
    requested_sizes: list[int] = []
    real_read = os.read
    real_fstat = os.fstat

    def bounded_read(fd: int, size: int) -> bytes:
        requested_sizes.append(size)
        return real_read(fd, size)

    def stale_small_fstat(fd: int) -> os.stat_result:
        values = list(real_fstat(fd))
        values[stat.ST_SIZE] = 1
        return os.stat_result(values)

    monkeypatch.setattr(source_module.os, "fstat", stale_small_fstat)
    monkeypatch.setattr(source_module.os, "read", bounded_read)

    with pytest.raises(ValueError, match="exceeds 2 MiB"):
        load_sources([path])

    assert requested_sizes == [MAX_SOURCE_BYTES + 1]


def test_source_loader_retries_short_reads_until_eof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "short-reads.txt"
    content = b"complete source content"
    path.write_bytes(content)
    requested_sizes: list[int] = []
    real_read = os.read

    def read_at_most_three_bytes(fd: int, size: int) -> bytes:
        requested_sizes.append(size)
        return real_read(fd, min(size, 3))

    monkeypatch.setattr(source_module.os, "read", read_at_most_three_bytes)

    loaded = load_sources([path])

    assert loaded.documents[0].content == content.decode("utf-8")
    assert loaded.documents[0].version == hashlib.sha256(content).hexdigest()
    assert requested_sizes[-1] == MAX_SOURCE_BYTES + 1 - len(content)


def test_source_loader_wraps_file_race_without_leaking_os_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "race.md"
    path.write_text("source-content-secret", encoding="utf-8")

    def fail_fstat(fd: int) -> os.stat_result:
        raise OSError("source-content-secret")

    monkeypatch.setattr(source_module.os, "fstat", fail_fstat)

    with pytest.raises(SourceLoadError) as caught:
        load_sources([path])

    assert "source-content-secret" not in str(caught.value)


def test_source_loader_accepts_a_file_exactly_two_mib(tmp_path: Path) -> None:
    path = tmp_path / "maximum.txt"
    content = b"a" * MAX_SOURCE_BYTES
    path.write_bytes(content)

    loaded = load_sources([path])

    assert loaded.documents[0].version == hashlib.sha256(content).hexdigest()


def test_source_loader_rejects_non_utf8_content(tmp_path: Path) -> None:
    path = tmp_path / "invalid.md"
    path.write_bytes(b"\xff")

    with pytest.raises(UnicodeDecodeError):
        load_sources([path])
