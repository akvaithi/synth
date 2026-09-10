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
    -- predicate normalised by predicates.key(). Supersession matches on THIS, not on the
    -- free-text predicate: keying on the string meant a fact rewritten under a marginally
    -- different name superseded nothing and both stayed live for ever.
    predicate_key TEXT,
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
-- What facts.live() actually looks up now.
CREATE INDEX IF NOT EXISTS idx_assertion_live_key
    ON assertion (entity_id, predicate_key) WHERE superseded_by IS NULL;

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
                    CHECK (status IN ('running','ok','error','skipped')),
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
    args_json     TEXT,                   -- what the tool was actually asked to do
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
    last_written_hash  TEXT,                   -- hash of what Notes STORED after the write
    last_written_at    TEXT,
    last_seen_hash     TEXT,                   -- hash observed on last poll
    last_edit_at       TEXT,                   -- when a user edit was last detected
    -- Hash of what Synth COMPOSED, before Notes touched it. Two hashes because Notes rewrites
    -- HTML on save, so these are never equal and each answers a different question:
    -- last_written_hash detects Arun's edits, last_render_hash detects whether re-rendering
    -- would change anything. Comparing across the two made every note look permanently
    -- changed and rewrote all of them every sweep, for ever.
    last_render_hash   TEXT
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

-- ---------------------------------------------------------------- connector auth

-- Nothing in these three tables is stored in a form that can be replayed. client_secret,
-- code and token all hold a SHA-256 digest of the value the client was given: this file sits
-- beside Arun's UIN, date of birth and home address, and a bearer token that can write to his
-- calendar should not be legible to anything that can read it.
CREATE TABLE IF NOT EXISTS oauth_client (
    client_id      TEXT PRIMARY KEY,
    client_secret  TEXT,                      -- sha256 of the secret handed out at registration
    name           TEXT,
    redirect_uris  TEXT NOT NULL,
    registered_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS oauth_code (
    code           TEXT PRIMARY KEY,          -- sha256 of the code in the redirect
    client_id      TEXT NOT NULL,
    redirect_uri   TEXT NOT NULL,             -- the code is bound to it; /token re-checks
    challenge      TEXT,                      -- PKCE; /authorize refuses to issue without one
    challenge_method TEXT,                    -- S256 only
    subject        TEXT,
    expires_at     TEXT NOT NULL,
    used           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS oauth_token (
    token          TEXT PRIMARY KEY,          -- sha256 of the access token
    client_id      TEXT NOT NULL,
    subject        TEXT,
    issued_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at     TEXT,
    revoked_at     TEXT,
    -- Single-use: redeeming one revokes this row and issues a fresh pair. Rotation is what
    -- makes theft visible -- the thief's first use breaks the real client.
    refresh_token  TEXT,                      -- sha256 of the refresh token
    refresh_expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_oauth_refresh
    ON oauth_token (refresh_token) WHERE refresh_token IS NOT NULL;


-- ---------------------------------------------------------------- cost control
--
-- What a sender has ever been worth. Synth spent four days paying Sonnet to read marketing
-- mail in full and conclude it was marketing mail; a sender that has never once produced an
-- action does not deserve a model run. Sightings and actions are counted here so the
-- demotion is evidence, not a guess, and `policy` records what was decided about it.
CREATE TABLE IF NOT EXISTS sender_policy (
    address        TEXT PRIMARY KEY,          -- lowercased bare address
    display        TEXT,
    policy         TEXT NOT NULL DEFAULT 'unknown'
                     CHECK (policy IN ('unknown','ignore','digest','consider','urgent')),
    decided_by     TEXT,                      -- which rule set it: 'static:no-reply', 'learned', 'manual'
    sightings      INTEGER NOT NULL DEFAULT 0,
    actions        INTEGER NOT NULL DEFAULT 0,
    last_seen_at   TEXT,
    first_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Mail that was filtered before any model saw it. Nothing is discarded: the next brief reads
-- this table and names every message, so hard filtering never means invisible.
CREATE TABLE IF NOT EXISTS mail_digest (
    id             INTEGER PRIMARY KEY,
    message_id     TEXT UNIQUE,
    account        TEXT,
    sender         TEXT,
    subject        TEXT,
    received_at    TEXT,
    verdict        TEXT NOT NULL,             -- ignore / digest
    decided_by     TEXT NOT NULL,             -- the rule that decided it
    mail_index     INTEGER,                   -- position in the mailbox, to fetch links later
    links          TEXT,                      -- destination URLs, extracted without a model
    reported_at    TEXT,                      -- when a brief last named it
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS mail_digest_unreported
    ON mail_digest (reported_at) WHERE reported_at IS NULL;


-- ---------------------------------------------------------------- local inference
--
-- What Synth depends on and whether it is there. Background reasoning runs on models on
-- Arun's own network, reached through a Cloudflare Access TCP tunnel, so a second machine is
-- now in the critical path and it can be absent without anything looking wrong.
--
-- `since` moves only when `up` flips. Overwriting it on every probe is how a warning that is
-- supposed to fire after sustained downtime never fires at all: the outage is always one
-- probe old.
CREATE TABLE IF NOT EXISTS service_health (
    service          TEXT PRIMARY KEY,          -- 'ollama' | 'synthd' | 'claude'
    up               INTEGER NOT NULL,
    checked_at       TEXT NOT NULL,
    since            TEXT,                      -- when it entered the state it is in now
    last_ok_at       TEXT,
    consecutive_bad  INTEGER NOT NULL DEFAULT 0,
    detail           TEXT                       -- version when up, the error when down
);

-- ---------------------------------------------------------------- semantic index
--
-- FTS5 answers "which document contains this word". It cannot answer "which document is
-- ABOUT this", which is what search_context is usually asked -- _fts_escape strips
-- punctuation and ORs every term, so a four-word question matches anything holding any one
-- of them. Embeddings answer the second question and are bad at the first: an exact
-- identifier, a course number, a person's name are what FTS is for. Both run, and the
-- document results are fused rather than one replacing the other.
CREATE TABLE IF NOT EXISTS embedding (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('document','assertion','entity','predicate')),
    ref_id       INTEGER NOT NULL,
    chunk        INTEGER NOT NULL DEFAULT 0,
    start_char   INTEGER NOT NULL DEFAULT 0,
    chars        INTEGER NOT NULL DEFAULT 0,
    -- Hash of the chunk, so a hit can be shown without re-reading the file and a re-embed
    -- can prove the source has not moved underneath it. Deliberately NOT source.content_hash:
    -- ocr_pass rewrites the cached text while the file's own hash stays exactly the same.
    text_hash    TEXT NOT NULL,
    model        TEXT NOT NULL,                 -- e.g. 'nomic-embed-text/prefixed'
    dims         INTEGER NOT NULL,
    vector       BLOB NOT NULL,                 -- float32 little-endian, dims * 4 bytes
    embedded_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kind, ref_id, chunk)
);

CREATE INDEX IF NOT EXISTS idx_embedding_ref ON embedding (kind, ref_id);

-- What still needs embedding. A backfill over 1,924 documents runs through a model that
-- serves one request at a time, so it must be resumable, and a document that cannot be
-- embedded must not be retried for ever.
CREATE TABLE IF NOT EXISTS embedding_queue (
    kind        TEXT NOT NULL,
    ref_id      INTEGER NOT NULL,
    text_hash   TEXT NOT NULL,
    enqueued_at TEXT NOT NULL DEFAULT (datetime('now')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    done_at     TEXT,
    PRIMARY KEY (kind, ref_id)
);

CREATE INDEX IF NOT EXISTS idx_embedding_queue_pending
    ON embedding_queue (enqueued_at) WHERE done_at IS NULL;

-- ---------------------------------------------------------------- reacting
--
-- Detected events waiting to be acted on. This was a JSON file, which cannot express
-- "claimed, in flight" -- and the worker and the sweep are separate processes, so that is
-- exactly how one event gets handled twice.
--
-- Rows are marked done, never deleted, so "was this message ever reacted to" is a query
-- rather than a guess. prune-state drops them once they are old.
CREATE TABLE IF NOT EXISTS reaction_queue (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,                 -- mail_new / note_edited / eventkit_changed
    -- The idempotency marker. mail_new:<messageId>; note_edited:<noteId>:<live_hash>, so a
    -- second, different edit is new work while a re-detection of the same one is not.
    dedupe_key   TEXT NOT NULL UNIQUE,
    payload      TEXT NOT NULL,
    enqueued_at  TEXT NOT NULL DEFAULT (datetime('now')),
    claimed_at   TEXT,
    claimed_by   INTEGER,                       -- pid, so a dead claimant is recognisable
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    done_at      TEXT,
    run_id       INTEGER REFERENCES run_log(id)
);

CREATE INDEX IF NOT EXISTS idx_reaction_pending
    ON reaction_queue (enqueued_at) WHERE done_at IS NULL;

-- Every note Synth has written, and the hash Notes actually STORED afterwards.
--
-- notes_mirror keeps this for the six rendered documents. This keeps it for everything else
-- Synth writes -- briefs, create_note, append_note -- because poll_notes reads any note whose
-- live hash differs from what Synth last wrote as a correction from Arun. Without this row a
-- note Synth wrote itself comes back as a correction nobody made, which holds the mirror and
-- files a phantom edit. The hash is of what Notes returned after the write, never of what was
-- composed: Notes rewrites HTML on save and the two are never equal.
CREATE TABLE IF NOT EXISTS synth_note (
    note_id      TEXT PRIMARY KEY,
    folder       TEXT NOT NULL,
    name         TEXT,
    written_hash TEXT NOT NULL,
    written_at   TEXT NOT NULL DEFAULT (datetime('now')),
    action_id    INTEGER REFERENCES action_log(id),
    purpose      TEXT                           -- 'brief' | 'note' | 'mirror'
);

-- Whether a document is about Arun at all.
--
-- Enrichment used to run over a curated set of about seventy files, so this question never
-- arose. Local inference makes it affordable to consider all 1,924 -- and that is exactly
-- when it starts to matter, because the corpus is mostly NOT about him: CHEN 201 and POLS 207
-- textbooks, lab handouts, other people's papers. Asked to extract "facts about Arun" from a
-- thermodynamics chapter, a model will find some, record them at high confidence with a real
-- source, and supersede true facts with them. The Superseded/ resume incident is the same
-- failure arriving from a different direction.
--
-- Cached because the answer does not change unless the file does, and asking is the cheap
-- half of the pass.
CREATE TABLE IF NOT EXISTS enrich_gate (
    document_id  INTEGER PRIMARY KEY REFERENCES document(id) ON DELETE CASCADE,
    relevant     INTEGER NOT NULL,
    why          TEXT,
    text_hash    TEXT NOT NULL,
    model        TEXT,
    at           TEXT NOT NULL DEFAULT (datetime('now'))
);
