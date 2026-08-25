"""Deciding what deserves a model, without using one.

The reactor spent four days paying Sonnet to read marketing email in full and then say so:

    "This is a generic Rec Sports promotional newsletter -- no personal obligation."
    "Confirmed noise -- a mass newsletter from a photo-preset product."
    "Chase transactions ($4.87 SOMISOMI, $35.67 Sushi Masa) -- routine spending alerts."

Each of those cost about thirty cents and thirteen turns. A regex on the sender would have
done it for nothing.

So the first pass is pure Python and free. It has four outcomes:

    urgent    -- act now, skip the batching window entirely
    consider  -- worth a cheap subject-line look from Haiku
    digest    -- named in the next brief, never given a model run
    ignore    -- named in the next brief in one line, nothing more

Nothing is discarded. `digest` and `ignore` both land in `mail_digest`, which the brief reads,
because Arun's standing rule is that nothing flies under the radar.

What makes the urgent rules trustworthy is the context database. It already knows that
`chen-studentservices@tamu.edu` and `natlfellows@tamu.edu` are people who write about his
applications, and it holds his advisors as entities. Deciding what deserves attention is the
job a personal-context database is actually for, and until now Synth never used it that way.
"""
from __future__ import annotations

import json
import re

from synth import config, db

# ---------------------------------------------------------------- parsing senders

_ADDR = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")


def bare_address(sender: str) -> str:
    """The address out of `"Texas A&M Rec Sports" <recsports@rec.tamu.edu>`."""
    m = _ADDR.search(sender or "")
    return m.group(0).lower() if m else ""


def display_name(sender: str) -> str:
    name = re.sub(r"<[^>]*>", "", sender or "").strip().strip('"').strip()
    return name


def domain(address: str) -> str:
    return address.rsplit("@", 1)[-1] if "@" in address else ""


# ---------------------------------------------------------------- static rules

# Local parts that announce the sender does not want a reply. A mailbox that cannot receive
# an answer cannot be waiting for one.
NO_REPLY = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|notifications?|mailer[-_.]?daemon|"
    r"bounces?|postmaster|automated|noreply)", re.IGNORECASE)

# Bulk senders, taken from Arun's actual mailbox rather than guessed.
BULK_DOMAINS = {
    "e2ma.net", "email.amazonses.com", "amazonses.com", "instructure.com",
    "maestro.it.tamu.edu", "mailchimp.com", "mcsv.net", "mcdlv.net", "sendgrid.net",
    "sendgrid.com", "mandrillapp.com", "sparkpostmail.com", "constantcontact.com",
    "cmail19.com", "cmail20.com", "createsend.com", "hubspotemail.net", "hubspot.com",
    "klaviyomail.com", "braze.com", "exacttarget.com", "salesforce.com", "marketo.com",
    "eventbrite.com", "meetup.com", "substack.com", "beehiiv.com",
}

# Subjects that are their own explanation. Deliberately conservative: these describe the
# message completely, so there is nothing a model could add by reading further.
NOISE_SUBJECT = re.compile(
    r"\b(unsubscribe|newsletter|webinar|\d+% off|flash sale|limited time|"
    r"black friday|cyber monday|last chance to save|shop now|"
    r"your (order|package|shipment) (has|is)|out for delivery|"
    r"transaction alert|your card ending|payment (received|posted|due reminder)|"
    r"statement is (now )?available|balance alert)\b", re.IGNORECASE)

# Routine account security mail. Named in the brief, never given a run -- the reactor already
# read eight of these in one batch and concluded they were routine sign-ins.
SECURITY_SUBJECT = re.compile(
    r"\b(new sign[- ]?in|security alert|was this you|verify your (email|identity)|"
    r"password (was )?changed|two[- ]factor|verification code|granted access to)\b",
    re.IGNORECASE)

# Automated job-alert blasts. Arun applies to a great many things and has said plainly that
# tracking every one of these is a waste; the brief names them, nothing reads them.
JOB_ALERT = re.compile(r"job[-_.]?alerts?|jobs?[-_.]?noreply|talent[-_.]?alerts?",
                       re.IGNORECASE)
JOB_ALERT_SUBJECT = re.compile(
    r"\b(is hiring for|are hiring|new jobs? (for|matching)|\d+ (new )?(jobs?|roles?)\b|"
    r"roles? went live|jobs? you may be interested|recommended for you|"
    r"your job alert|new opportunities matching)\b", re.IGNORECASE)

# Recurring bulk mail-outs. The learned demotion catches the long tail after a few sightings;
# these catch the obvious ones on first contact.
NEWSLETTER = re.compile(r"newsletter|digest@|weekly@|updates?@|marketing@|info@updates",
                        re.IGNORECASE)
NEWSLETTER_SUBJECT = re.compile(
    r"\b(monthly highlights|weekly (digest|roundup|update)|your daily dose|"
    r"this week (at|in)|newsletter|what's happening|upcoming events this)\b", re.IGNORECASE)

# An .ics on the message is a commitment being proposed. Always worth a look.
INVITE_SUBJECT = re.compile(
    r"\b(invitation|invite|accepted:|declined:|updated invitation|meeting request|"
    r"calendar invite|rsvp)\b", re.IGNORECASE)


# ---------------------------------------------------------------- context from the database


def context(conn) -> dict:
    """Everything the rules need, gathered once per batch rather than per message.

    Addresses are mined out of assertion text because that is where they already live -- the
    database holds an explicit "recurring correspondents for application evidence" assertion
    naming the people who write to Arun about live applications.

    But being written down is not the same as being trusted. `seniordev88590@gmail.com` is in
    the database precisely because Synth investigated it and found a recruiter whose story
    did not check out; treating a mention as a vouch would have sent it straight to the front
    of the queue. So a mined address earns a look, never a bypass -- only a person entity
    does that.
    """
    addresses: set[str] = set()
    for (text,) in conn.execute(
            "SELECT value_text FROM assertion "
            "WHERE superseded_by IS NULL AND value_text LIKE '%@%'"):
        addresses.update(a.lower() for a in _ADDR.findall(text or ""))
    addresses -= {a.lower() for a in config.MAIL_ACCOUNTS.values()}
    # Automated senders get in here too: application portals quote their own no-reply address.
    addresses = {a for a in addresses
                 if not NO_REPLY.match(a.split("@", 1)[0]) and domain(a) not in BULK_DOMAINS}

    people = {r["name"].lower() for r in conn.execute(
        "SELECT name FROM entity WHERE kind = 'person'")}
    people -= {"arun vaithianathan"}

    # Distinctive words from live obligations. A token shared with many obligations says
    # nothing -- "application", "research" and "meeting" appear across half of them -- so only
    # words that identify one commitment are kept.
    from synth.agenda import tokens
    titles = [tokens(r["title"]) for r in conn.execute(
        "SELECT title FROM obligation WHERE status IN ('open','waiting')")]
    freq: dict[str, int] = {}
    for t in titles:
        for w in t:
            freq[w] = freq.get(w, 0) + 1
    obligations = [{w for w in t if freq[w] == 1 and len(w) >= 5} for t in titles]
    obligations = [t for t in obligations if t]

    policy = {r["address"]: {"policy": r["policy"], "decided_by": r["decided_by"]}
              for r in conn.execute("SELECT address, policy, decided_by FROM sender_policy "
                                    "WHERE policy != 'unknown'")}
    return {"addresses": addresses, "people": people,
            "obligations": obligations, "policy": policy}


# ---------------------------------------------------------------- the decision


def classify(msg: dict, ctx: dict) -> tuple[str, str]:
    """One message to a verdict and the rule that produced it.

    Order matters. The urgent rules run first: `chen-studentservices@tamu.edu` looks like a
    no-reply address and is in fact one of the people Arun most needs to hear from.
    """
    sender = msg.get("sender") or ""
    subject = msg.get("subject") or ""
    addr = bare_address(sender)
    name = display_name(sender).lower()
    policy = ctx["policy"].get(addr, {}) if addr else {}
    decided = policy.get("decided_by") or ""
    manual = decided.startswith("manual")

    # --- urgent: a person the database knows by name, and nothing else.
    # Urgent means "skip the batching window and spend Sonnet now", so it has to stay rare.
    # Everything else that looks interesting goes to `consider`, where a cheap subject-line
    # pass decides -- at worst that costs half an hour of delay, not a wrong expensive run.
    for person in ctx["people"]:
        last = person.split()[-1]
        if person in name or (len(last) > 3 and last in name):
            return "urgent", f"known person {person}"
    if policy.get("policy") == "urgent":
        return "urgent", f"sender policy: urgent ({decided})"

    # --- a decision Arun made by hand outranks everything below it
    if manual and policy.get("policy") in ("ignore", "digest", "consider"):
        return policy["policy"], f"sender policy: {policy['policy']} ({decided})"

    # --- content that earns a look regardless of who sent it.
    # This has to sit above the learned policy. A sender demoted for three dull messages can
    # still send a fourth that matters -- Shell sends three "application received" notices and
    # then an interview invitation -- and swallowing that would be the whole system failing at
    # the one job it has.
    if INVITE_SUBJECT.search(subject):
        return "consider", "may be a calendar invitation"
    subject_tokens = _subject_tokens(subject)
    for ob in ctx["obligations"]:
        shared = subject_tokens & ob
        if len(shared) >= 2:
            return "consider", f"matches an open obligation on {sorted(shared)}"
    if addr and addr in ctx["addresses"]:
        return "consider", f"address known to the database ({addr})"

    # --- learned policy: this sender has been seen and has never been worth anything
    if policy.get("policy") in ("ignore", "digest", "consider"):
        return policy["policy"], f"sender policy: {policy['policy']} ({decided})"

    # --- static noise
    local = addr.split("@", 1)[0] if addr else ""
    dom = domain(addr)
    if dom in BULK_DOMAINS or any(dom.endswith("." + b) for b in BULK_DOMAINS):
        return "digest", f"bulk sender domain {dom}"
    if JOB_ALERT.search(sender) or JOB_ALERT_SUBJECT.search(subject):
        return "digest", "automated job-alert blast"
    if NEWSLETTER.search(sender) or NEWSLETTER_SUBJECT.search(subject):
        return "digest", "newsletter or bulk announcement"
    if SECURITY_SUBJECT.search(subject):
        return "digest", "routine account security notice"
    if NOISE_SUBJECT.search(subject):
        return "ignore", "subject is self-explanatory marketing or transactional mail"
    if local and NO_REPLY.match(local):
        return "digest", f"no-reply mailbox ({local}@)"

    # --- everything else earns a cheap subject-line look
    return "consider", "unknown sender, subject worth a look"


def _subject_tokens(subject: str) -> set[str]:
    from synth.agenda import tokens
    return tokens(subject)


def split(conn, messages: list[dict]) -> dict:
    """Classify a batch and record the sightings. Returns the messages grouped by verdict."""
    ctx = context(conn)
    out: dict[str, list[dict]] = {"urgent": [], "consider": [], "digest": [], "ignore": []}
    for m in messages:
        verdict, why = classify(m, ctx)
        m = dict(m, verdict=verdict, decided_by=why)
        out[verdict].append(m)
        record_sighting(conn, m)
    conn.commit()
    return out


# ---------------------------------------------------------------- learning


def record_sighting(conn, msg: dict) -> None:
    addr = bare_address(msg.get("sender") or "")
    if not addr:
        return
    conn.execute(
        "INSERT INTO sender_policy (address, display, sightings, last_seen_at) "
        "VALUES (?,?,1,?) ON CONFLICT (address) DO UPDATE SET "
        "  sightings = sender_policy.sightings + 1, last_seen_at = excluded.last_seen_at, "
        "  display = COALESCE(sender_policy.display, excluded.display)",
        (addr, display_name(msg.get("sender") or "") or None, db.now()))


def credit_action(conn, sender_or_address: str) -> None:
    """This sender was worth reading. Recorded so learning cannot demote them."""
    addr = bare_address(sender_or_address) or (sender_or_address or "").lower()
    if not addr:
        return
    conn.execute(
        "UPDATE sender_policy SET actions = actions + 1, policy = 'consider', "
        "decided_by = 'learned: produced an action', updated_at = ? WHERE address = ?",
        (db.now(), addr))
    conn.commit()


def learn(conn) -> dict:
    """Demote senders that have been seen repeatedly and never once been worth anything.

    Only ever demotes to `digest`, never to `ignore`: the evidence says this sender has not
    mattered yet, not that it can never matter, and a digest line still puts it in front of
    Arun.
    """
    ctx = context(conn)
    demoted = []
    rows = conn.execute(
        "SELECT address, display, sightings FROM sender_policy "
        "WHERE policy IN ('unknown','consider') AND actions = 0 AND sightings >= ?",
        (config.DEMOTE_AFTER_SIGHTINGS,)).fetchall()
    for r in rows:
        # Never demote someone the database vouches for.
        if r["address"] in ctx["addresses"]:
            continue
        name = (r["display"] or "").lower()
        if any(p in name or p.split()[-1] in name for p in ctx["people"]):
            continue
        conn.execute(
            "UPDATE sender_policy SET policy = 'digest', decided_by = ?, updated_at = ? "
            "WHERE address = ?",
            (f"learned: {r['sightings']} messages, none ever actionable", db.now(),
             r["address"]))
        demoted.append(r["address"])
    conn.commit()
    return {"demoted": demoted}


# ---------------------------------------------------------------- the digest


def hold(conn, messages: list[dict]) -> int:
    """Park filtered mail where the next brief will find it."""
    n = 0
    for m in messages:
        conn.execute(
            "INSERT INTO mail_digest (message_id, account, sender, subject, received_at, "
            "verdict, decided_by, mail_index) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT (message_id) DO NOTHING",
            (m.get("messageId"), m.get("account"), m.get("sender"), m.get("subject"),
             m.get("receivedAt"), m.get("verdict", "digest"), m.get("decided_by", ""),
             m.get("index")))
        n += 1
    conn.commit()
    return n


def unreported(conn, limit: int = 80) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, message_id, account, sender, subject, received_at, verdict, "
        "decided_by, mail_index, links "
        "FROM mail_digest WHERE reported_at IS NULL ORDER BY received_at DESC LIMIT ?",
        (limit,))]


def fill_links(conn, rows: list[dict], cap: int = 12) -> None:
    """Pull destination URLs out of filtered mail without spending a token.

    Arun's rule is that a brief hands him the link rather than the email. Letting the model
    fetch them cost a whole brief: it spent thirty-one turns calling mail_links per message,
    hit its turn limit and produced nothing at all. Parsing HTML is Python's job.

    Indexes are resolved fresh rather than trusted from when the message was filed, because
    a mailbox index moves every time mail arrives. The daemon verifies the Message-ID and
    refuses a mismatch, so a stale index is safe -- it simply returns nothing, which is the
    quiet failure that leaves a brief linkless for no visible reason.
    """
    from synth import maillinks
    from synth.applekit import call

    want = [r for r in rows if r.get("links") is None and r["verdict"] == "digest"][:cap]
    if not want:
        return
    index: dict[str, dict[str, int]] = {}
    for account in {r["account"] for r in want if r["account"]}:
        try:
            index[account] = {m["messageId"]: m["index"]
                              for m in call("mail_recent", account=account,
                                            limit=config.MAIL_SCAN_LIMIT * 2, timeout=300)
                              if m.get("messageId")}
        except Exception:
            index[account] = {}
    for r in want:
        i = index.get(r["account"], {}).get(r["message_id"]) or r.get("mail_index")
        urls = []
        if i:
            try:
                found = maillinks.extract(r["account"], i, r["message_id"])
                urls = [l["url"] for l in (found.get("links") or [])][:4]
            except Exception:
                urls = []
        payload = json.dumps(urls)
        conn.execute("UPDATE mail_digest SET links = ?, mail_index = COALESCE(?, mail_index) "
                     "WHERE id = ?", (payload, i, r["id"]))
        r["links"] = payload
    conn.commit()


def mark_reported(conn, ids: list[int] | None = None) -> int:
    if ids:
        q = f"UPDATE mail_digest SET reported_at = ? WHERE id IN ({','.join('?' * len(ids))})"
        cur = conn.execute(q, [db.now(), *ids])
    else:
        cur = conn.execute(
            "UPDATE mail_digest SET reported_at = ? WHERE reported_at IS NULL", (db.now(),))
    conn.commit()
    return cur.rowcount
