# Data contracts

A **data contract** is a versioned document that declares which columns of the study
schema may leave a hospital, and under what conditions. Nothing in this directory is code.
These files are the agreement itself: a hospital's governance committee reads one, and if
it agrees, records its acceptance in `acceptance/<node>.yaml`.

Every query against the gateway names exactly one contract. The gateway releases only what
that contract permits, from only the hospitals that accepted it.

```
contracts/
  clinical-full-access.yaml     the contracts themselves
  fetal-cardiac-outcomes.yaml
  population-health.yaml
  grants.yaml                   which users may invoke which contracts
  acceptance/
    bch.yaml  mgh.yaml  bwh.yaml    what each hospital agreed to
```

## Anatomy of a contract

```yaml
contract: fetal-cardiac-outcomes     # stable id, kebab-case, matches the filename
version: 1.2.0                       # semver; a new version is a new agreement
title: Fetal and cardiac MR outcomes study
purpose: >                           # prose, for humans; states the limits of use
  Retrospective analysis of fetal and cardiac MR findings. Publication only;
  no re-identification, no linkage against external datasets.
expires: 2027-06-30                  # after this date the contract stops working

row_scope: BodyPartExamined in ["FETAL", "HEART"]    # optional; see below

k_anonymity:                         # optional; see below
  k: 5
  quasi_identifiers: [PatientSex, InstitutionName, BodyPartExamined, PatientAge]

rules:
  - deny: [PatientName, PatientID, PatientBirthDate, StudyInstanceUID]
    reason: HIPAA Safe Harbor §164.514(b)(2) direct identifiers

  - allow: [SourceNode, InstitutionName, Modality, BodyPartExamined,
            StudyDate, PatientSex, PatientAge]

  - allow: Diagnosis
    when: BodyPartExamined == "FETAL"
    reason: fetal reports carry lower re-identification risk
```

`allow` and `deny` each take a single column name or a list. `reason` is free text and is
never interpreted — it exists so the next reader knows *why*, and it is surfaced in the API
when a column is withheld.

### Evaluation is default-deny, and deny always wins

A column not named by any rule is **not released**. A column named by both an `allow` and a
`deny` is **not released**, whatever the order. There is no precedence puzzle to reason
about: you cannot accidentally widen a contract by appending a rule at the bottom.

### `when:` — conditional release

`allow` with a `when:` releases the column only on rows where the predicate holds. In the
example above, `Diagnosis` appears on fetal studies and is absent on cardiac ones, in the
same result set.

The predicate language is deliberately tiny: column names, literals, `and` / `or` / `not`,
the comparisons `== != < <= > >=`, and `in` / `not in` against a list. No function calls, no
attribute access, no arithmetic. It is parsed as an expression and checked against a
whitelist of syntax nodes before it ever runs — a contract cannot execute code.

```yaml
when: BodyPartExamined == "FETAL"
when: PatientSex == "F" and BodyPartExamined in ["FETAL", "HEART"]
when: StudyDate >= "20260101"
```

**A conditionally-released column cannot be searched, filtered, or sorted on.** Such a query
is rejected with 400. A per-row condition has no coherent answer at whole-query level: asking
to sort by a column that exists on only some rows would leak which rows those are.

### `row_scope` — which rows exist at all

`row_scope` takes the same predicate language and applies to the whole contract. Rows that
fail it are not returned, not counted in `total`, and not included in any statistic. They
are invisible rather than forbidden — a query under this contract cannot distinguish
"no such row" from "a row you may not see", which is what stops the API being used to probe
for the existence of records outside the study population.

## `k_anonymity` — never release a row that describes one person

Removing names does not make a record anonymous. What identifies someone is the *combination*
of attributes left behind. Exactly one record in this dataset is a 34-year-old female with a
fetal study at Boston Children's — no name, no MRN, no birthdate, and still uniquely
identified to anyone who knew she was pregnant and being seen there.

`k_anonymity` refuses to release such a row:

```yaml
k_anonymity:
  k: 5
  quasi_identifiers: [PatientSex, InstitutionName, BodyPartExamined, PatientAge]
```

Rows are grouped by their quasi-identifier values, and any group with fewer than `k` members
is withheld entirely. The 34-year-old is alone in her group, so she is never returned. The 55
twenty-year-olds with fetal studies at the same hospital are each hidden among the other 54, so
they are.

### Choosing quasi-identifiers is the whole game

A quasi-identifier is an attribute that is harmless alone, identifying in combination, **and
knowable from outside this dataset** — sex, age, site, body part. `Diagnosis` is not one; it
is the thing being protected.

The counter-intuitive part: **a finer list protects fewer people, not more.** Adding a
high-cardinality column fragments the population into groups of one and suppresses nearly
everything, which looks strict and helps nobody. Measured on the current 2,700 records:

| Quasi-identifiers | k | Released | Withheld |
| --- | --- | --- | --- |
| `PatientSex, InstitutionName, BodyPartExamined` | 5 | 2,700 | 0 — protects no one |
| **`… + PatientAge`** | **5** | **1,984** | **716** |
| `… + PatientAge` | 20 | 521 | 2,179 |
| `… + StudyDate` | 20 | 0 | 2,700 — releases nothing |

**Measure before committing a list.** The table above took one script over `data/*.json`; the
difference between a useful contract and a useless one was one column.

### Suppression is not neutral

The withheld rows are disproportionately neonates, anatomical outliers, and rare
presentations — frequently the exact population a rare-disease study needs. k-anonymity
protects the vulnerable by making them invisible to research, and that cost cannot be
engineered away. It can only be chosen deliberately, which is why `k` belongs in a document a
hospital committee signs rather than in code. A study that genuinely needs rare cases should
be a separate contract, argued on its own terms.

### What the engine refuses

A contract that promises k-anonymity but cannot deliver it is rejected at load time:

- **A quasi-identifier that is not unconditionally released.** Grouping on a column the caller
  never sees constrains nothing.
- **k-anonymity alongside any free-text column.** A radiology report identifies its subject by
  itself, so no group-size guarantee over structured columns survives releasing one. Deny
  `Diagnosis`, or drop the `k_anonymity` block.

### What it changes at query time

- `GET /api/studies/{node}/{study_id}` is **refused entirely** (`403`). One row is a group of
  one. The refusal happens before the hospital is contacted and is identical for a study that
  does not exist, so it leaks nothing.
- `total` counts only rows that survived, and `/api/stats` counts the same released dataset.
- Aggregate cells smaller than `k` are suppressed. If only one cell would be dropped, the
  next-smallest goes too — otherwise the missing value is just `total` minus the rest.
- Responses carry a `k_anonymity` block reporting **whether** rows were withheld, never how
  many. A count would let the caller compute the true number of people matching their filter.

### What it does not protect against

k-anonymity is evaluated per query and is **not composable across queries**. Two overlapping
requests can be differenced to isolate someone that neither would reveal alone. Real defence
needs query-history auditing, which this gateway does not do. Nor does k-anonymity address
*homogeneity*: a group of five who share the same diagnosis discloses that diagnosis for all
five, which is what ℓ-diversity exists to address.

## The columns you can name

The twelve DICOM fields the hospital nodes serve — `PatientName`, `PatientID`,
`PatientBirthDate`, `PatientAge`, `PatientSex`, `InstitutionName`, `StudyID`,
`StudyInstanceUID`, `StudyDate`, `Modality`, `BodyPartExamined`, `Diagnosis` — plus columns
the gateway or the labeling pipeline derive:

| Column | Derived from | Kind |
| --- | --- | --- |
| `SourceNode` | — | gateway-added (`"BCH"`) |
| `FederatedID` | `SourceNode`, `StudyID` | **reversible** (`"BCH:BR-7214"`) |
| `GenericCategory` | `Diagnosis` | **declassifying** |
| `FindingTags` | `Diagnosis` | **declassifying** |

The distinction matters and the loader enforces it.

A **reversible** derived column contains its sources verbatim. `FederatedID` is literally
`StudyID` with the node glued on, so releasing it while denying `StudyID` would hand over
the denied column under another name. A contract that tries this is **rejected at load
time**, loudly, rather than silently leaking.

A **declassifying** derived column is lossy by construction. `GenericCategory` reduces a
multi-paragraph radiology report to one of nine values. Releasing it *while* denying
`Diagnosis` is the entire point of the labeling pipeline, so this is allowed and is exactly
what `population-health.yaml` does.

Contracts may name `GenericCategory` and `FindingTags` before the labeling pipeline has run.
The loader warns that the column is unavailable and carries on; it never blocks startup.

## Grants — who may use a contract

`grants.yaml` maps a username to the contracts they may query under. Acceptance and grants
are independent checks, and both must pass:

- not granted → **403**, regardless of what the hospitals accepted
- granted, but no hospital accepted → **403**
- granted, some hospitals accepted → **200**, `partial: true`, with the abstaining hospitals
  reported as `not_accepted` in `sources`

## Why acceptance pins a digest

`acceptance/<node>.yaml` records a `sha256` of the exact contract file the committee
reviewed. On startup the gateway recomputes it. If the bytes have changed, that hospital's
acceptance no longer applies — the contract was edited after it was agreed to, so it is a
different agreement.

This is what makes a *contract* rather than a config file: the terms cannot drift out from
under the party that signed them. Fixing a typo in a purpose statement invalidates the
acceptance, which is correct and intended. To change a contract in any way:

1. Bump `version:` (semver — any change to `rules:` or `row_scope:` is at least a minor).
2. Re-run the digest and update each hospital's `acceptance/<node>.yaml` as they re-approve.
3. Update `grants.yaml`, since grants name an exact `contract@version`.

Hospitals that have not re-approved simply keep serving the old version. Two versions of a
contract can be live at once; requesters name the one they mean.

```bash
sha256sum contracts/fetal-cardiac-outcomes.yaml
```

## Adding a contract

1. Write `contracts/<id>.yaml`. Start from `population-health.yaml` — deny-heavy contracts
   are easier to review than allow-heavy ones.
2. Compute the digest, add it to each accepting hospital's `acceptance/<node>.yaml`.
3. Add the `contract@version` to the relevant users in `grants.yaml`.
4. Restart the gateway. It validates every contract at startup and reports the resolved
   column set for each; a contract that fails validation is rejected with a message naming
   the rule at fault.
5. Confirm what it actually releases:

   ```bash
   curl -s "localhost:8000/api/contracts/<id>@<version>" -H "Authorization: Bearer $TOKEN" | jq
   ```

## What contracts deliberately do not cover

- **Which hospitals** a requester may reach. That is the hospitals' decision, expressed by
  accepting or declining, not something a contract author can assert.
- **Who** the requester is. There are no role or identity conditions inside a contract; a
  contract describes data, and `grants.yaml` describes people.
- **Transformations** — binning ages, truncating dates, redacting names out of free text.
  The format reserves room for these but the engine does not implement them yet; a contract
  using one is rejected rather than silently ignored. This interacts with `k_anonymity`:
  binning `PatientAge` into decades would collapse today's 546 groups into a handful of large
  ones, so the same `k` would suppress far fewer people. Transforms and k-anonymity are worth
  revisiting together.
