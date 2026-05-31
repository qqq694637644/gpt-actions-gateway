from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
import zlib

from app.services.commits import _apply_git_delta, _decode_git_binary_payload, _parse_file_section


def _init_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="gap-test-"))
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
    return repo


def test_parse_mode_only_patch() -> None:
    repo = _init_repo()
    (repo / "m.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "m.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "update-index", "--chmod=+x", "m.txt"], cwd=repo, check=True, capture_output=True)

    patch = subprocess.run(["git", "diff", "--cached"], cwd=repo, check=True, capture_output=True).stdout.decode("latin1")
    parsed = _parse_file_section("m.txt", "m.txt", patch.splitlines(keepends=True)[1:])

    assert parsed.operation == "modified"
    assert parsed.old_mode == "100644"
    assert parsed.new_mode == "100755"
    assert parsed.hunks == []


def test_decode_binary_literal_patch() -> None:
    repo = _init_repo()
    content = bytes(range(256))
    (repo / "d.bin").write_bytes(content)
    subprocess.run(["git", "add", "d.bin"], cwd=repo, check=True, capture_output=True)

    patch = subprocess.run(["git", "diff", "--cached", "--binary"], cwd=repo, check=True, capture_output=True).stdout.decode("latin1")
    parsed = _parse_file_section("d.bin", "d.bin", patch.splitlines(keepends=True)[1:])

    assert parsed.binary_forward is not None
    assert parsed.binary_forward.kind == "literal"
    restored = zlib.decompress(_decode_git_binary_payload(parsed.binary_forward.lines))
    assert restored == content


def test_apply_binary_delta_patch() -> None:
    repo = _init_repo()
    original = os.urandom(2048)
    (repo / "d.bin").write_bytes(original)
    subprocess.run(["git", "add", "d.bin"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    updated = bytearray(original)
    for i in range(20):
        updated[i] ^= 0xFF
    (repo / "d.bin").write_bytes(updated)

    patch = subprocess.run(["git", "diff", "--binary"], cwd=repo, check=True, capture_output=True).stdout.decode("latin1")
    parsed = _parse_file_section("d.bin", "d.bin", patch.splitlines(keepends=True)[1:])

    assert parsed.binary_forward is not None
    assert parsed.binary_forward.kind == "delta"
    delta = zlib.decompress(_decode_git_binary_payload(parsed.binary_forward.lines))
    restored = _apply_git_delta(original, delta, "d.bin")
    assert restored == bytes(updated)
