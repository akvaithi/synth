-- Synth context database. Source of truth, VM-only, never in iCloud Drive.
--
-- Principles:
--   * Store pointers and extracted facts, never mirrored content. Mail bodies, event bodies
--     and file contents stay where they live; we keep identifiers.
--   * Link to Apple items by stored identifier, never by title match.
--   * Never update an assertion. Supersede it, so history and provenance survive.
--   * Every write and every run is logged. Troubleshooting is a query, not archaeology.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- provenance

CREATE TABLE IF NOT EXISTS source (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN
                    ('mail','event','reminder','file','note','conversation','web','manual')),
    native_id     TEXT NOT NULL,          -- Message-ID, EventKit id, path, note id, URL
    detail        TEXT,                   -- subject / filename / short label, no bodies
    content_hash  TEXT,                   -- for files and notes, to detect change
    seen_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kind, native_id)
);

-- ---------------------------------------------------------------- typed core

CREATE TABLE IF NOT EXISTS entity (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN
                    ('person','org','program','course','application','project','award','topic')),
    name          TEXT NOT NULL,
    description   TEXT,
    status        TEXT,                   -- e.g. active / submitted / declined / complete
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kind, name)
);

CREATE TABLE IF NOT EXISTS edge (
    id            INTEGER PRIMARY KEY,
    src_id        INTEGER NOT NULL REFERENCES entity(id),
    dst_id        INTEGER NOT NULL REFERENCES entity(id),
    relation      TEXT NOT NULL,          -- applied_to, advises, prereq_of, satisfies, ...
    source_id     INTEGER REFERENCES source(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (src_id, dst_id, relation)
);

-- ---------------------------------------------------------------- loose edge

-- Never UPDATE a row here. To correct a fact, insert a new one and set superseded_by
-- on the old one. That is what makes "when did this change, and what told me" answerable.
CREATE TABLE IF NOT EXISTS assertion (
    id            INTEGER PRIMARY KEY,
    entity_id     INTEGER REFERENCES entity(id),
    predicate     TEXT NOT NULL,
    value_text    TEXT,
    value_num     REAL,
    value_date    TEXT,
    source_id     INTEGER REFERENCES source(id),
    confidence    REAL NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
    observed_at   TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_by INTEGER REFERENCES assertion(id),
    mail_derived  INTEGER NOT NULL DEFAULT 0   -- 1 = came from mail; treat as lower trust
);
CREATE INDEX IF NOT EXISTS idx_assertion_live
    ON assertion (entity_id, predicate) WHERE superseded_by IS NULL;

-- ---------------------------------------------------------------- actionable

CREATE TABLE IF NOT EXISTS obligation (
    id             INTEGER PRIMARY KEY,
    title          TEXT NOT NULL,
    due            TEXT,
    status         TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open','done','dropped','waiting')),
    entity_id      INTEGER REFERENCES entity(id),
    source_id      INTEGER REFERENCES source(id),
    -- Link by identifier, never by title. Renaming in Reminders must not break this.
    ek_identifier  TEXT UNIQUE,
    ek_kind        TEXT CHECK (ek_kind IN ('reminder','event')),
    externally_set INTEGER NOT NULL DEFAULT 0,  -- 1 = real external deadline, 0 = self-set
    resolved_by    INTEGER REFERENCES source(id), -- the mail that answered it, if any
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Extracted URLs, so briefs hand you the destination instead of the email.
CREATE TABLE IF NOT EXISTS link (
    id            INTEGER PRIMARY KEY,
    url           TEXT NOT NULL,
    title         TEXT,
    kind          TEXT,                   -- posting / portal / form / deadline / other
    entity_id     INTEGER REFERENCES entity(id),
    source_id     INTEGER REFERENCES source(id),
    first_seen    TEXT NOT NULL DEFAULT (datetime('now')),
    dismissed     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (url, source_id)
);

-- ---------------------------------------------------------------- audit

CREATE TABLE IF NOT EXISTS run_log (
    id            INTEGER PRIMARY KEY,
    job           TEXT NOT NULL,          -- brief / reactor / watch / interview / manual
    trigger       TEXT,                   -- what caused it
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','ok','error')),
    summary       TEXT,
    detail        TEXT                    -- reasoning, decisions, errors
);

CREATE TABLE IF NOT EXISTS action_log (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER REFERENCES run_log(id),
    at            TEXT NOT NULL DEFAULT (datetime('now')),
    action        TEXT NOT NULL,          -- create_reminder / complete_reminder / ...
    target_kind   TEXT NOT NULL,          -- reminder / event / note / file / db
    target_id     TEXT,                   -- EventKit id, note id, path, row id
    reason        TEXT NOT NULL,          -- why Synth did this, in plain words
    evidence_id   INTEGER REFERENCES source(id),  -- the mail that justified it
    before_json   TEXT,                   -- prior state, for undo
    after_json    TEXT,
    undone_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_at ON action_log (at DESC);

-- ---------------------------------------------------------------- notes mirror

-- The Notes folder is a two-way surface: Synth renders documents into it, and edits you
-- make there are read back as corrections. last_written_hash is what Synth last wrote;
-- if the live note differs, you changed it.
CREATE TABLE IF NOT EXISTS notes_mirror (
    id                 INTEGER PRIMARY KEY,
    doc                TEXT NOT NULL UNIQUE,   -- logical document name
    note_id            TEXT UNIQUE,            -- Notes.app x-coredata id
    note_name          TEXT,
    last_written_hash  TEXT,                   -- hash of normalised text Synth wrote
    last_written_at    TEXT,
    last_seen_hash     TEXT,                   -- hash observed on last poll
    last_edit_at       TEXT                    -- when a user edit was last detected
);

-- ---------------------------------------------------------------- search

-- External-content FTS: the index mirrors the real tables and is kept in sync by triggers,
-- so no caller has to remember to update it. Contentless tables (content='') were tried
-- first and rejected -- they do not support UPSERT, which made every write a special case.

CREATE VIRTUAL TABLE IF NOT EXISTS entity_fts USING fts5(
    name, description, content='entity', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS entity_ai AFTER INSERT ON entity BEGIN
    INSERT INTO entity_fts (rowid, name, description) VALUES (new.id, new.name, new.description);
END;
CREATE TRIGGER IF NOT EXISTS entity_ad AFTER DELETE ON entity BEGIN
    INSERT INTO entity_fts (entity_fts, rowid, name, description)
    VALUES ('delete', old.id, old.name, old.description);
END;
CREATE TRIGGER IF NOT EXISTS entity_au AFTER UPDATE ON entity BEGIN
    INSERT INTO entity_fts (entity_fts, rowid, name, description)
    VALUES ('delete', old.id, old.name, old.description);
    INSERT INTO entity_fts (rowid, name, description) VALUES (new.id, new.name, new.description);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS assertion_fts USING fts5(
    predicate, value_text, content='assertion', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS assertion_ai AFTER INSERT ON assertion BEGIN
    INSERT INTO assertion_fts (rowid, predicate, value_text)
    VALUES (new.id, new.predicate, new.value_text);
END;
CREATE TRIGGER IF NOT EXISTS assertion_ad AFTER DELETE ON assertion BEGIN
    INSERT INTO assertion_fts (assertion_fts, rowid, predicate, value_text)
    VALUES ('delete', old.id, old.predicate, old.value_text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS link_fts USING fts5(
    url, title, content='link', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS link_ai AFTER INSERT ON link BEGIN
    INSERT INTO link_fts (rowid, url, title) VALUES (new.id, new.url, new.title);
END;
CREATE TRIGGER IF NOT EXISTS link_ad AFTER DELETE ON link BEGIN
    INSERT INTO link_fts (link_fts, rowid, url, title) VALUES ('delete', old.id, old.url, old.title);
END;

-- ---------------------------------------------------------------- document text

-- Extracted text lives here so it is searchable alongside everything else. Bodies of mail
-- are deliberately NOT stored -- those stay in Mail and we keep only the Message-ID -- but
-- files are ours to index, and searching them is how "what did I write about X" gets answered.
CREATE TABLE IF NOT EXISTS document (
    id         INTEGER PRIMARY KEY,
    source_id  INTEGER NOT NULL UNIQUE REFERENCES source(id),
    path       TEXT NOT NULL,
    title      TEXT,
    text       TEXT NOT NULL,
    chars      INTEGER NOT NULL,
    indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
    title, text, content='document', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS document_ai AFTER INSERT ON document BEGIN
    INSERT INTO document_fts (rowid, title, text) VALUES (new.id, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS document_ad AFTER DELETE ON document BEGIN
    INSERT INTO document_fts (document_fts, rowid, title, text)
    VALUES ('delete', old.id, old.title, old.text);
END;

-- ---------------------------------------------------------------- enrichment tracking

-- Which documents have already been through LLM extraction. Extraction is the expensive
-- stage, so a re-run must resume rather than start over.
CREATE TABLE IF NOT EXISTS enrichment (
    document_id INTEGER PRIMARY KEY REFERENCES document(id),
    run_id      INTEGER REFERENCES run_log(id),
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);
