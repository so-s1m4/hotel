# Guest parser: source time grounding

Parser `1.0.5` and prompt `guest-semantics-v2` strengthen how a requested time is
grounded in the guest's original words. The public schema stays at `1.0`, source
fields remain separate from extracted intent, and the
[15-case benchmark fixture](../tests/fixtures/parser_benchmark_cases.jsonl) is
unchanged. The implementation is in
[`ingestion/guest_parser.py`](../ingestion/guest_parser.py).

## Observed failure and acceptance boundary

The bounded pilot at commit `2961124` included this German request:

> Können wir morgen oder übermorgen um 13:00 früher einchecken?

The recorded Ministral 8B response split the single alternative-date request into
two `early_checkin` objects: January 6 and January 7 at `12:00Z`, both without
review. The message was sent on January 5, 2026, at `10:00Z`. Its original response
SHA-256 is
`a888f78ca0dc78b66b95cd98aa9df8aa8f5a537a4bbe7f7640b3be79e3c496a1`;
the exact response is retained in the
[regression module](../tests/ingestion/test_parser_time_ambiguity.py).

Two host rules allowed those definite dates to survive. A narrow time quote took
precedence over the wider evidence containing both alternatives. Also,
`morgen um 13:00` passed literal substring checking because it occurred inside
`übermorgen um 13:00`. The shorter phrase described a different day.

The acceptance boundary is that a quote cannot choose one of the source's
unresolved date alternatives or obtain a different date by cutting a supported
relative-date expression. Separate requests with independently supported dates must still
retain their own timestamps.

## Host behavior

The guard examines occurrences in the original source before accepting a date
from a narrow quote:

- A quote beginning inside a source word, or cutting an already-supported
  relative-date phrase, is not a complete occurrence. This includes German/Dutch compound suffixes and
  shortened English, French and Spanish two-day phrases. If a valid complete
  occurrence also exists, an unrelated fragment does not invalidate it.
- Complete occurrences retain nearby date alternatives, including when the model
  quotes only the clock or returns several intent objects. Different or
  unresolved alternative dates leave UTC null and require review.
- Complete occurrences that disagree about their source date also remain
  unresolved when the quote and evidence cannot distinguish them.
- Sentence, semicolon and newline boundaries separate date scopes. An additive
  conjunction starts a new date scope only when the following clause has its own
  date expression. A clock-only clarification such as `und zwar` does not detach
  a clock from its preceding date alternatives.
  Commas and dots inside numeric dates do not break that association.
- The existing clock, timezone, booking-causality and review rules remain in
  force. A known booking date cannot settle an explicit alternative-date request.

The host preserves the model's intent objects. For the captured two-object
response, both requested timestamps become null and both objects require review;
the host does not silently merge them into one request. The frozen benchmark
expects one unresolved intent, so this regression repair alone does **not** make
that captured response pass the benchmark's cardinality check.

## Prompt and DST alignment

The revised prompt asks for one unresolved intent when one request offers
alternative dates, while retaining genuinely independent requests. It also
distinguishes an unresolved autumn clock repetition from one disambiguated by
an explicit valid zone:

| Vienna-local request | Requested UTC |
|---|---|
| October 25, 2026, `02:30`, no zone | Null: two occurrences remain possible. |
| October 25, 2026, `02:30 CET` | `2026-10-25T01:30:00Z` |
| October 25, 2026, `02:30 CEST` | `2026-10-25T00:30:00Z` |
| March 29, 2026, `02:30` in Vienna | Null: this local clock time does not exist. |

CET/CEST does not make a nonexistent Vienna-local time valid. The host also
continues to preserve a model's explicit `requested_time_utc=null` together with
`needs_review=true`. Consequently, the pilot's uncertain CET response is not
retroactively converted into a successful model prediction by this change.
The prompt clarification aligns future instructions with the existing explicit
CET gold case; no new model-quality result is implied.

## Cache and benchmark identity

The production cache key includes the parser version, prompt version and prompt
text. The move from `1.0.4` / `guest-semantics-v1` to `1.0.5` /
`guest-semantics-v2` therefore prevents reuse of old extraction keys.

The benchmark manifest additionally records parser-source and prompt digests.
An old warm-run marker does not match this parser/prompt identity. Start a fresh
cold run with a new candidate/run identity before making a matching warm
comparison. Preserve the historical pilot reports with their original version
and digests; follow the [benchmark guide](PARSER_BENCHMARK.md) for execution and
complete-report requirements.

## Regression evidence and limits

The [targeted tests](../tests/ingestion/test_parser_time_ambiguity.py) inject the
captured response and bounded synthetic controls. They cover selective date
quotes, shortened supported date phrases, explanatory conjunctions, independent
requests sharing a clock, and genuine/disambiguated DST cases. Their network
guards prevent actual model transport. Running a captured response through new
host code establishes a host regression result, not another model prediction.

These guards use the parser's existing date vocabulary and bounded source-scope
rules; they are not a general natural-language proof of intent or chronology.
Literal evidence occurrence does not by itself establish semantic entailment,
correct negation handling, or an instruction-only message's proper intent.
The schema, benchmark gold, approval requirements and absence of real actuation
are unchanged. See the [frozen contract](../common/01_CONTRACT.md).
