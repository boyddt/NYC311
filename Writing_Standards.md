# Writing Standards

DRAFT for the Owner's review. Standing rules for every document the NYC311 team
writes. Rules 1.1 to 1.4 carry over from the Owner's earlier writing-rules export.
Rule 1.5, rule 1.6 and sections 2 to 6 are new and unreviewed.

## 0. How to use this

- Read section 1 before writing anything. It applies to every document.
- Then read the section for the type of document you are writing (sections 2 to 6).
  If your document fits none of them, follow section 1 and say so in your report.
- Every type section has the same four parts: purpose and reader, required contents,
  format and location, and checks before handoff. Run the checks before you report
  the file as done.
- When a rule or a fact is missing, stop and ask the Owner. Do not guess.
- This repository is public. Anything in a tracked file is visible to employers and
  to anyone else.

---

## 1. Rules for every document

### 1.1 No em dashes
No em dashes in any written output. Use a comma, colon, semicolon or parentheses,
whichever the sentence needs. This covers documents, READMEs, reports, code
comments, commit messages and chat summaries.

Older files in this repository predate the rule. Do not rewrite them in bulk. Fix
the em dashes in any file you are already editing.

### 1.2 No inflated or fabricated claims
- Never fabricate or embellish. Every claim traces to a file, a query result or a
  source you checked.
- Every statement must survive the question "tell me more about that." If the Owner
  could not answer it, the statement does not belong.
- When data is missing, write a marked placeholder in the form
  `[INSERT: description]`. Never substitute an invented number or tool name. Flag
  real gaps; do not paper over them.
- Keep approximations approximate: "about", "roughly" or "an estimated". No
  superlatives without support.

### 1.3 Verify before you state
- Before a factual, numeric or regulatory claim goes into a document, check it
  against the primary source: the database, the API, the code, or the regulation
  itself. Do not state from memory what a rule or a system does.
- Give each figure its date and how it was obtained, for example "22,715,022 rows,
  counted 2026-10-07". A number with no date goes stale without anyone noticing.
- Say which statements you confirmed and which you inferred. Say what you did not
  check.
- Before reporting anything about time, confirm the data covers the period (see
  "Data currency" in CLAUDE.md). A thin recent month is usually missing data, not
  a drop in complaints.
- Run a command or test before you document its output. Quote the real result.

### 1.4 Banned words and patterns
Rewrite any sentence that contains these. Do not swap in a lazy substitute.

- **Never use:** move the needle; paradigm shift; outside the box; proactive;
  low-hanging fruit; circle back; touch base; boil the ocean; hit the ground
  running; synergy; the bottom line; thought leadership; mission-critical; full
  transparency; game changer; raise the bar; push the envelope; run the numbers;
  reinvent the wheel; giving 110%; at the end of the day; it is what it is; viral;
  look under the hood; lots of moving parts; par for the course; trim the fat;
  drink the Kool Aid; hope is not a strategy; 80/20 rule.
- **Overused buzzwords, replace with plain words:** delve, foster, streamline,
  beacon, testament, underscore, facilitate, passionate, leverage, utilize,
  dynamic, innovative, results-driven, hardworking, detail-oriented, self-starter,
  seasoned, impactful, robust, best-in-class, world-class, cutting-edge,
  transformative, disruptive, strategic. Prefer use, apply, improve, simplify.
- **Also avoid:** arena, arsenal, cadence, capture, compelling, craft, critical,
  crucial, deep dive, dive, elevate, embark, employ, engage, enhance, harness, hone,
  navigate, nail, nimble, realm, resonate, revolutionize, tackle, tap, uncover,
  unleash, unlock, unveil, supercharge, skyrocket, secret sauce, fast-paced.
- **Cliches:** "I am a team player"; "goes above and beyond"; "proven track
  record"; "value-add"; "strong communication skills"; guru, ninja, wizard,
  rockstar.
- **Performative openers and empty descriptors:** "Excited to share", "Humbled",
  "Thrilled", "Delighted"; "results-driven professional", "dynamic team player",
  "strategic thinker", "data-driven decision-making"; vague superlatives such as
  "extensive experience", "deep expertise", "wide range of skills".
- **Sentence patterns, rewrite the whole sentence:** "It's not about X, it's about
  Y"; "It's not just X. It's Y."; "That's not X, that's Y."; "Not because X. But
  because Y."; "No X. No Y. Just Z."; three-adjective staccato fragments; "And the
  X? Y." and "The result? ..."; "X is more than just Y"; "Not just X, but Y"; "The
  question isn't X, it's Y"; "What if I told you X"; "Here's the thing about X";
  "The truth is, X"; "That's where X comes in"; "Whether you're X or Y, Z"; "In a
  world where X, Y"; "X changes everything"; "If you're struggling with X"; "My
  journey in X"; "It goes without saying."
- **Technical terms of art are exempt.** A word on these lists is fine when it is
  the precise term in context: change data capture, dynamic SQL, a critical
  section, a robust retry. The lists target filler, not vocabulary the subject
  requires.

### 1.5 Plain, specific writing
- Lead with the point. The first sentence of a document or section says what the
  reader most needs.
- Define an abbreviation on first use. Name the table, column, file or function
  instead of "the data" or "the script".
- Prefer a short sentence and the concrete noun. Cut a sentence that tells the
  reader nothing they could act on.
- Report a failure with its actual output. Do not soften it, and do not leave it
  out.

### 1.6 Keep private things out of tracked files
Credentials, tokens, option files, the Owner's private email address, and home
directory paths do not go in any tracked file. Refer to `~/.my.cnf` or
`NYC_APP_TOKEN` by name, never by value. Write paths relative to the project root.

---

## 2. README and top-level documents

**Purpose and reader.** A README is read by a stranger deciding in a minute whether
the project is worth their time: a hiring manager, a collaborator, a reviewer.
`CLAUDE.md` is different: its reader is the team, and it is the operating manual.

**Required contents (README).**
1. What the project is, in two sentences.
2. What is interesting or hard about it, with the evidence.
3. Layout: a table of the main paths and what each is.
4. How to run it, with commands that work.
5. Status and known limits, stated plainly.

**Format and location.** Markdown. `README.md` at the project root. Headings in
sentence case. Tables for layout; prose for reasoning.

**Checks before handoff.**
- Every command in the README ran, and the output matches what it says.
- Every count, row total and date was re-measured, or carries its measurement date.
- No home-directory paths, credentials or private addresses (rule 1.6).
- It reads correctly to someone who has never seen CLAUDE.md.

---

## 3. Data dictionary

**Purpose and reader.** An analyst or engineer who needs to know exactly what a
column holds before using it in a query or a join.

**Required contents.** One entry per column, grouped by table:

| Field | Meaning |
|---|---|
| Column | Name exactly as in the database |
| Type | MariaDB type and length |
| Nullable | Yes or no, and the share of rows that are NULL, with the date measured |
| Meaning | One plain sentence |
| Values | Allowed values, units or format; for a lookup, the lookup table |
| Source | The API field it comes from, from the field mapping |
| Notes | Quirks: collation, trimmed values, known bad data, indexes, foreign keys |

Start the document with the table's row count and date range, each with its
measurement date.

**Format and location.** Markdown, one table per database table, in
`output/democritus/`. Name it `democritus_YYYY-MM-DD_data_dictionary.md`.

**Checks before handoff.**
- Types, nullability, keys and indexes were read from `information_schema` or
  `SHOW CREATE TABLE`, not retyped from memory.
- NULL shares and value lists come from queries run on the date stated.
- Every column in the table appears exactly once; none is invented.
- A column whose meaning you could not confirm says "unconfirmed", not a guess.

---

## 4. Code review

**Purpose and reader.** The author of the code, deciding what to fix and in what
order. The existing reviews in `output/plato/` and `output/democritus/` are the
model.

**Required contents.**
1. Header: the date reviewed, the file or script reviewed, and its size or
   revision.
2. Findings ordered by severity, most serious first. Use these labels: **Blocker**
   (it fails or corrupts data), **Defect** (wrong result or unsafe behavior),
   **Smaller points** (style, clarity, efficiency).
3. For each finding: what is wrong, the evidence (file and line, or a reproduced
   result), the effect, and a proposed fix.
4. A closing recommendation: what to do first.

**Format and location.** Markdown in the reviewer's directory under `output/`. Name
it `<agent>_YYYY-MM-DD_<subject>_review.md`.

**Checks before handoff.**
- Each finding says whether it is **confirmed** (you reproduced it) or **inferred**
  (you read the code and reasoned). Do not present an inference as a result.
- Line numbers match the code on the review date.
- A proposed fix is tested, or marked untested.
- Code blocks are copied from the source, not retyped.
- You said what you did not review.

---

## 5. Design and reference notes

**Purpose and reader.** Someone who must make or understand a technical decision
later: why the loader pages by `created_date`, what the API returns, how fields map.

**Required contents.**
1. A first line stating what was verified, how and when, for example "Verified live
   2026-08-26. Every figure below came from a real request."
2. The decision or the facts, ahead of the history of how you found them.
3. The reasoning, and the alternatives you rejected with the reason.
4. Open questions and known limits.

**Format and location.** Markdown in `output/plato/` unless a specialist owns the
topic. Name it `<agent>_YYYY-MM-DD_<topic>.md`.

**Checks before handoff.**
- Each figure has its source and date, or is marked as an estimate.
- Commands and queries were run; the output quoted is the real output.
- A fact that changes (row count, endpoint behavior, schedule) carries the date it
  was true.
- If the note supersedes an earlier one, it says which.

---

## 6. Analysis report or findings

**Purpose and reader.** A person who will act on, or repeat, the result. They need
the answer, how much to trust it, and how to reproduce it.

**Required contents.**
1. The question, and the answer in the first paragraph.
2. Data coverage: the date range and row counts used, and the result of the
   data-currency check.
3. Method: the filters, joins and definitions, including how a metric is computed.
4. Results with counts, not only percentages.
5. Caveats: what the data cannot tell you, and what you did not check.
6. The queries, so anyone can reproduce the numbers.

**Format and location.** Markdown, with charts as image files beside it, in
`output/aristotle/`. Name it `aristotle_YYYY-MM-DD_<topic>.md`. Build charts with the
`dataviz` skill.

**Checks before handoff.**
- The data-currency check ran, and the report says what it found.
- Every number in the text matches the query output it came from.
- A trend claim states the period compared and the number of rows in each period.
- A result that looks like a collapse or a spike was checked against missing data
  before it was reported as a finding.
- The queries in the report are the ones that produced the figures.

---

## 7. Not yet covered

No standard exists yet for these. Follow section 1, keep to the style of nearby
files, and tell the Owner if you think one is needed.

- Runbooks and incident write-ups
- Commit messages (current practice: a short imperative subject line, no prefix)
- Code comments and docstrings
