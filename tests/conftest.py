"""Fixtures for the Synth tests.

Everything here builds against a temporary database created from sql/schema.sql. Nothing in
this suite touches synth.db, the real Documents folder, or synthd -- a test run on this VM
must be incapable of changing anything of Arun's, or it is not something anyone will run.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = os.path.join(ROOT, "sql", "schema.sql")


@pytest.fixture
def db_path(tmp_path) -> str:
    """Where the temporary database lives, for tests that need more than one connection."""
    return str(tmp_path / "test.db")


@pytest.fixture
def conn(db_path) -> sqlite3.Connection:
    """An empty database with the current schema.

    Built by executing sql/schema.sql directly rather than by copying synth.db, so a run of
    this suite also proves the schema file still builds -- the thing that would otherwise
    only be discovered on a fresh install.
    """
    from synth import db

    c = db.connect(db_path)
    with open(SCHEMA) as f:
        c.executescript(f.read())
    c.commit()
    return c


@pytest.fixture
def migrated(conn):
    """A database that has been through db.migrate() once."""
    from synth import db

    db.migrate(conn)
    return conn


@pytest.fixture
def documents(tmp_path, monkeypatch):
    """A stand-in Documents root with the writable folder in place.

    docwrite resolves against a module-level DOCUMENTS taken from config at import time, so
    the redirection has to happen on the module attribute rather than on the environment.
    """
    from synth import config, docwrite

    root = tmp_path / "Documents"
    writable = root / config.WRITABLE_DOCUMENTS
    writable.mkdir(parents=True)
    monkeypatch.setattr(docwrite, "DOCUMENTS", str(root))
    monkeypatch.setattr(docwrite, "VERSIONS", str(tmp_path / "docversions"))
    return root
