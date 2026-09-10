Extract hard facts from the documents listed below and record them.

These are Arun's own files. Treat them as authoritative for what they actually state — but
only for what they actually state.

## What to extract

Prefer facts that will still matter in six months and that a person would have to look up:

- **Degree and coursework**: requirements, courses taken and planned, credit hours, GPA if
  stated, catalog year, degree plan constraints.
- **Programs, scholarships, applications**: names, deadlines, eligibility rules, submission
  status, outcomes.
- **Record**: awards, roles, positions held, competitions, publications, declined offers.
- **People**: advisors, professors, mentors, recommenders, and what they relate to.
- **Projects**: what was built, what stage it reached, honest scope.

Skip anything transient, anything you would not be able to justify from the text, and
anything that is a draft of an opinion rather than a fact.

## How to record it

- Use `add_facts`, **one call per document**, always passing that document's `document_id`.
  That is what links each fact back to the file that stated it.
- **Entity names must be canonical and reusable.** "Goldwater Scholarship", not "the
  Goldwater". If an entity plausibly already exists, call `search` first and reuse
  the exact existing name, otherwise you will create duplicates that are painful to merge.
- **Confidence is meaningful.** 1.0 only for something the document states outright. Lower it
  for anything you inferred, and say so in the predicate or description.
- **Honest scope is binding.** If a document says a system was simulated or bench-tested, do
  not record it as deployed or tested in the field. If a competition was design-and-pitch,
  record "designed" or "proposed", never "built". Under-claim rather than smooth.
- Dates go in `date` as ISO 8601. Numbers go in `num`. Everything else in `text`.
- Record the honest status on entities: submitted, awarded, declined, in progress, abandoned.

## Conflicts

If a document contradicts something already in the database, record the new fact anyway —
supersession keeps both and preserves the history. Note the contradiction in your summary so
it can be raised with Arun rather than silently resolved.

## Documents

{documents}

Read each with `document`. When you are done, summarise in a few lines: what you
recorded, what you deliberately skipped, and anything that contradicted existing data.
