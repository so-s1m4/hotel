# PLATO-40 v2 | Team handoff (13–14 Nov 2026)

**Purpose:** Two independent Codex / Claude Code sessions must implement separate modules that fit together without renegotiating the interface.

- **Person A, Лёша:** data ingestion, time-causal replay access, multilingual guest-request parsing (LLM), normalized event stream. Owns `ingestion/`.
- **Person B, Максим:** anomaly detection, prioritization, decision planning, approval/safety gate, mock execution, official `decisions.jsonl`, insights. Owns `engine/`, `run.py`, final integration.
- **Shared and FROZEN:** `common/contracts/schemas.py`, `common/contracts/api.py`, `common/01_CONTRACT.md` and examples. **Do not independently modify these.** If anything really must change, update once in the shared repository, version the contract, and tell the other person.

## Source hierarchy
1. **Updated v2:** `PLATO40_v2_CHALLENGE.pdf`, the zip's `CHALLENGE.md`, `README.md`, `DECISION_CONTRACT.md` and actual CSV/JSON contents.
2. **Older:** `Case_Book_00_All_Themes (1).docx` is only context; it conflicts on deadline and is superseded by v2 for the Rhomberg track. Confirm final deadline on Discord.
3. **Our design:** `NormalizedEventV1` and `ReplayDataGateway` are team internal, **not** official organizer schemas. `DecisionRecordV2` is grounded in the official v2 `DECISION_CONTRACT.md`.

## What to extract first
Unzip `PLATO40_v2_quickstart.zip` to create a `PLATO40_v2_quickstart/` folder. Keep `PLATO40_v2_dev.zip` separate and unzip it separately. **Do not concatenate datasets**: they are independent synthetic runs.

```bash
python -m pip install -r common/requirements-contract.txt
PYTHONPATH=. pytest -q common/tests
```

The `common/examples/normalized_event_early_checkin.json` is derived from a real Quickstart guest request `GR000005` and its booking `BK000173`, with an example LLM extraction. `common/examples/decision_example.json` is **illustrative**, not a claim that scheduling was possible at that instant.

## Safety and submission

- Offline historical replay, not real robot or hotel control. Mark simulated actions as simulated.
- Never release a dirty/occupied/blocked room or a room still requiring an inspection; robots cannot certify cleanliness.
- Never schedule sick/unavailable staff, use future observations, or treat BMS alarms as truth.
- Technician dispatch, room block, booked guest reassignment and notable cost need human approval; occupied-room entry and water shutoff additionally require guest consent.
- **v2 CHALLENGE deadline: Sat 14 Nov, 14:00 sharp**, versus older Case Book 15:00. Confirm with organizer, prepare for 14:00.
- **Holdout expected ~11:00 Nov 14**; before release create Git tag `pre-holdout`, run same pipeline unchanged after release. Encrypted `.7z` supplied here is NOT decrypted and not used for development.
- Final root: `LICENSE` (MIT), `README.md`, `.env.example`, `decisions.jsonl`, `decisions_holdout.jsonl`, `insights.md`. Also submit 3-minute video and the written pitch.

**Dataset credit:** PLATO-40 synthetic dataset, Proximata Hackathon '26 – Rhomberg track (CC BY 4.0). The building is fictional.
