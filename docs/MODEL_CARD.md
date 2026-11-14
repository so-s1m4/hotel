# Local guest parser: candidate evidence

No candidate is certified for autonomous hotel operation or 100% accuracy.
The local model extracts requests and source quotations; deterministic host
validation resolves times and retains uncertainty. Neither a model label nor a
review flag grants approval, consent, room access or permission to send messages.

## Native training and comparison, 14 November 2026

The pinned base is `mlx-community/Ministral-3-3B-Instruct-2512-4bit`, revision
`a962dcb09eee4169c890e544c9eb938f1113fdee`. This is an MLX 4-bit artifact;
results are not interchangeable with an Ollama/GGUF build of a similarly named model.

Two independently trained adapters were compared against that base on the same
Apple M4 CPU10/GPU8 host with 16 GiB RAM, MLX 0.32.4 and MLX-LM 0.32.2:

- Dev100: checkpoint SHA-256
  `3407a03e4c06d8e8314ed53ae3ee2c37d123178ca7d39ec509b857ee7bcb5d48`.
- Synthetic100: checkpoint SHA-256
  `6664423ca9aa66828835cb1072eddca6b69418417f01dc4521f1d1243cbb0f98`.
  A fresh-base 100-step run completed in 898.77 seconds. It did not resume Dev100.

Both adapters passed native finite-weight and exact-base key/shape load checks.
Those checks establish checkpoint compatibility, not understanding or accuracy.
The dedicated trainer's padding-loss mask was corrected and tested before
training; the installed patch and native preflight are recorded with the run.

| Diagnostic cohort | Cases | Base host passes | Dev100 host passes | Synthetic100 host passes |
| --- | ---: | ---: | ---: | ---: |
| Original directed cases | 15 | 7 | 14 | 12 |
| Local acceptance cases | 24 | 12 | 22 | 22 |
| Cleaning translations | 12 | 4 | 11 | 9 |
| Additional single-request cases | 30 | 12 | 25 | 24 |
| Additional multiple-request cases | 6 | 2 | 1 | 3 |

These are case-level host checks, not an overall accuracy estimate. The cleaning
cohort is one translated scenario family. The first 51 cases use frozen parser
1.2.2; the additional 36 use parser 1.2.4. All three candidates use the same
configuration within each cohort. The additional cases are development evidence
and have informed host fixes; they are not an untouched final test.

The comparison made 261 instrumented calls with no application-cache hits.
There were 259 normalized model paths and two reviewed fallbacks: Dev100 emitted
malformed JSON for one multiple-request case; Synthetic100 returned invalid
list-valued `details` for one case. MLX did not enforce the requested JSON format.
Base replies used JSON fences; a strict raw-format failure does not mean the
underlying request labels were all wrong.

Dev100 improves many single-request cases but omits independent requests and
misclassifies two service refusals without requesting review. Synthetic100 finds
both expected labels in all six multiple-request diagnostics, but three cases
still fail complete formatting/time/review checks. All 17 failed Synthetic100
cases have the extraction review flag set in the retained primary reports.
That observable metadata does not prove semantic correctness or future safety.

The [compact comparison receipt](reviews/local_model_primary_comparison.json)
records source, input, report and checkpoint identities without guest text.
Original reports and raw replies remain separate from later offline host
revalidation. Revalidating saved replies is not another model inference run.

## Coverage gap and next candidate

Approved Dev TRAIN has 161 single-request examples and no multiple-request
examples. Synthetic TRAIN has 240 single-request and 24 two-request examples,
covering only two intent pairs. Existing VALID has no multiple-request examples.
Adding repetitions alone does not establish broader semantic coverage.

A fresh-base Mixed200 candidate is training on a split-preserving mixture:
425 original TRAIN examples plus 68 separately reviewed TRAIN-only compositions.
The compositions are 34 forward/reverse pairs derived from 46 existing synthetic
TRAIN rows, not 68 independent new scenarios. Repeating them twice gives 561
training exposures and 493 unique inputs. All 48 original VALID examples remain
separate; the mixture contains no TEST rows. Exact native token checks found a
maximum of 1,510 tokens within the 1,536-token limit, with no target truncation.
The preselected endpoint is step 200; intermediate checkpoints are not candidates
to select using diagnostic answers. Dev31 and
the independently authored final multiple-request set remain reserved for one
selected, frozen configuration. Test examples must never become training data.
Training completion, lower validation loss and adapter load success are not
release acceptance criteria.

## Operational limits

The engine uses mock actuation and separate approval/consent checks. A full live
Quickstart and Dev replay with the selected final model is still required, with
raw-response accounting and independent output validation. Existing historical
submission artifacts do not represent these new adapters.

The engine is not a general dispatcher for every extracted guest-service label.
Guest-driven turnaround currently starts from source requests labelled
`early_checkin` or `waiting_for_room`; a secondary extracted request does not
automatically start another service workflow. Wi-Fi, towels and other extracted
labels have no general guest-service dispatcher. Telemetry detectors operate
independently. Extraction coverage must not be reported as completed service
fulfilment.

The organizer Holdout, final submission and demo recording remain separate
release tasks. No statement here certifies them as complete.
