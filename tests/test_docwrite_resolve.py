"""The document-write containment boundary.

docwrite.resolve is the guard its own docstring describes as reachable by a model that has
just read a hostile email. Every refusal below is one an attacker would try, and the
parent-vs-leaf realpath subtlety in particular is the kind of thing a later refactor undoes
silently -- resolving the leaf instead of the parent lets a symlinked directory through,
because realpath of a not-yet-existing file succeeds without complaint.
"""
from __future__ import annotations

import os

import pytest

from synth import config, docwrite

GOOD = f"{config.WRITABLE_DOCUMENTS}/notes.md"


def test_accepts_a_plain_path_in_the_writable_folder(documents):
    resolved = docwrite.resolve(GOOD)
    assert resolved == str(documents / config.WRITABLE_DOCUMENTS / "notes.md")
    assert docwrite.relative(resolved) == GOOD


def test_accepts_a_subfolder_of_the_writable_folder(documents):
    (documents / config.WRITABLE_DOCUMENTS / "sub").mkdir()
    assert docwrite.resolve(f"{config.WRITABLE_DOCUMENTS}/sub/x.md")


def test_normalises_before_recording_the_path(documents):
    """'markdown/./notes.md' and 'markdown/notes.md' must not become two source rows."""
    assert docwrite.relative(docwrite.resolve(
        f"{config.WRITABLE_DOCUMENTS}/./notes.md")) == GOOD


@pytest.mark.parametrize("path", ["", "   "])
def test_refuses_an_empty_path(documents, path):
    with pytest.raises(ValueError, match="path is required"):
        docwrite.resolve(path)


def test_refuses_an_absolute_path(documents):
    with pytest.raises(ValueError, match="absolute path"):
        docwrite.resolve("/etc/hosts")


def test_refuses_a_home_relative_path(documents):
    with pytest.raises(ValueError, match="absolute path"):
        docwrite.resolve("~/.ssh/authorized_keys")


def test_refuses_a_null_byte(documents):
    with pytest.raises(ValueError, match="null byte"):
        docwrite.resolve(f"{config.WRITABLE_DOCUMENTS}/x\x00.md")


@pytest.mark.parametrize("path", [
    f"{config.WRITABLE_DOCUMENTS}/",
    f"{config.WRITABLE_DOCUMENTS}/..",
    f"{config.WRITABLE_DOCUMENTS}/.",
])
def test_refuses_a_folder(documents, path):
    with pytest.raises(ValueError, match="names a folder"):
        docwrite.resolve(path)


@pytest.mark.parametrize("name", ["report.pdf", "resume.docx", "data", "notes.MD.exe"])
def test_refuses_an_extension_that_does_not_round_trip(documents, name):
    with pytest.raises(ValueError, match="cannot write"):
        docwrite.resolve(f"{config.WRITABLE_DOCUMENTS}/{name}")


@pytest.mark.parametrize("name", config.PROTECTED_DOCUMENTS)
def test_refuses_the_files_synth_reads_to_learn_what_it_is(documents, name):
    """A system that can edit its own instructions and read them back can talk itself
    into anything."""
    with pytest.raises(PermissionError, match="read-only"):
        docwrite.resolve(f"{config.WRITABLE_DOCUMENTS}/{name}")


def test_refuses_traversal_out_of_the_writable_folder(documents):
    with pytest.raises(PermissionError, match="outside the one folder"):
        docwrite.resolve(f"{config.WRITABLE_DOCUMENTS}/../../../escaped.md")


def test_refuses_a_sibling_folder_under_documents(documents):
    """The rest of Documents is read-only, not merely unmentioned."""
    (documents / "Personal").mkdir()
    with pytest.raises(PermissionError, match="outside the one folder"):
        docwrite.resolve("Personal/secret.md")


def test_refuses_a_symlinked_parent(documents, tmp_path):
    """realpath is taken on the PARENT for exactly this case: the leaf does not exist yet,
    so resolving the leaf would succeed and let the symlinked directory through."""
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, documents / config.WRITABLE_DOCUMENTS / "escape")
    with pytest.raises(PermissionError, match="outside the one folder"):
        docwrite.resolve(f"{config.WRITABLE_DOCUMENTS}/escape/x.md")


def test_refuses_a_symlinked_leaf(documents, tmp_path):
    target = tmp_path / "target.md"
    target.write_text("elsewhere")
    os.symlink(target, documents / config.WRITABLE_DOCUMENTS / "link.md")
    with pytest.raises(PermissionError, match="symbolic link"):
        docwrite.resolve(f"{config.WRITABLE_DOCUMENTS}/link.md")
