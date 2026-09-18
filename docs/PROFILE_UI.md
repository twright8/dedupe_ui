# Profile fields the frontend reads

`GET /api/profile` is the only place the shared frontend learns what a record is
called, what its child rows are called, and what the tool is measured against.
Every field below has a fallback, so a profile that has not declared one yet
still reads sensibly. Nothing in `src/` outside these fields names a dataset.

## Already in use

| Field | Type | Where it is used | Fallback when absent |
|---|---|---|---|
| `key` | string | picks the worked examples on the How it works page | no examples, generic wording |
| `title`, `subtitle` | string | page title, browser tab, How it works lede | — |
| `input` | `{label, extensions, help}` | the New run upload card | "Input file", `.csv` |
| `tracks` | `[{key, label}]` | every track chip, the Config tabs, the model panel | `person` / `organisation` |
| `display_columns` | `[{key, label, type}]` | every record and unit table, the diff view, the focus strip | one `name` column |
| `priority_columns` | `[key]` | the "highest first" sort on Review, Cluster review, Entities, Exact groups | no priority sort offered |
| `event_columns` | `[{key, label, type}]` | the evidence tables on Review and Cluster review | no evidence panel |
| `evidence_focus` | `[{id, label, when, record_columns, event_columns}]` | the "What to check for…" strip and the cluster members table | full column set, no strip |

`type` on a column is what decides formatting: `money` renders as sterling,
`number` and `year` right-align in the mono face, `list` shows the first value
and a `+N` chip, `date` orders the evidence tables. Nothing assumes a money
column exists.

## New, for the second profile

| Field | Type | Where it is used | Fallback when absent |
|---|---|---|---|
| `nouns.record` | string | not yet read; reserved for empty states | "record" |
| `nouns.record_plural` | string | not yet read; reserved for empty states | "records" |
| `nouns.unit_evidence` | string | the Evidence card subtitle on Review and Cluster review ("the {unit_evidence} behind each side") | "history" |
| `nouns.evidence_row` | string | the neutral pattern summary, singular | "row" |
| `nouns.evidence_row_plural` | string | "No {evidence_row_plural} recorded for this side", and the neutral pattern summary | "rows" |
| `pattern_summary` | `[{template, when?}]` | the one-line summary above each side's evidence table | a neutral line: the row count, then the year span when the unit carries `first_year` and `last_year` |
| `export_description` | string | the Publish & export tab, under the proposal buttons | a sentence naming only the record ID, the entity ID and how it was decided |
| `existing_label_name` | string or `null` | names the earlier grouping everywhere it is mentioned: the histogram's second stacking, the agreement table, the precision and recall sentences | "earlier manual grouping" |

### `pattern_summary`

Entries are tried in order and the first whose `when` conditions all hold is
used, exactly as `evidence_focus` and a track rule are chosen. `when` takes the
same five operators the browser can evaluate: `equals`, `in`, `is_null`,
`not_null`, `starts_with`, matched case-insensitively, with a null value never
satisfying a condition.

`template` is split on ` · `. Each segment may name unit columns as
`{column}` or `{column:format}`, where `format` is `money`, `percent`, `number`
or `year`. A segment naming a value the unit does not carry is dropped whole, so
`born {dob_month}/{dob_year}` disappears rather than printing `born /`. Nothing
is evaluated: a template can only name a column and pick a format.

```json
"pattern_summary": [
  { "template": "{n_donations} donations · usually {modal_value:money} · {share_round_1000:percent} round thousands · {first_year}–{last_year}" }
]
```

### `existing_label_name: null`

A profile with no imported grouping sets this to `null`. The frontend then
leaves out every mention of one: the "Imported labels" decided-by chip, the
"Earlier labels disagree" filter, the "By earlier decisions" stacking on the
histogram, the agreement table, and the "Mixed earlier IDs" wording. The chips
come back if the run's counts show imported decisions anyway, so a profile that
gains one later needs no frontend change.

## Paging

Every large table is paged on the server: records, pairs, exact groups,
clusters and entities all send `offset` and `limit`, and no screen asks for more
than 500 rows in one call. The one exception is the Label library, which reads
the newest 500 labels and filters them in the browser; it says so on screen when
there are more. A server-paged label list would remove that caveat.

## Declared by the two profiles

| Field | donations | psc |
|---|---|---|
| `nouns.record` / `_plural` | donor / donors | PSC record / PSC records |
| `nouns.unit_evidence` | donation history | companies controlled |
| `nouns.evidence_row` / `_plural` | donation / donations | company / companies |
| `existing_label_name` | earlier manual grouping | `null` — PSC has no imported grouping |
| `priority_columns` | `total_value` | `n_companies` |

`pattern_summary` entries are matched against the UNIT's own columns, so a
profile may branch on a column the unit carries. PSC uses `track` to tell a
person's line (companies, birth month and year, nationality, residence) from an
organisation's (companies, country of registration, registration number).
