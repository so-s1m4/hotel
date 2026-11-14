# PLATO 40: Demo

The site replays the original Quickstart and Dev datasets over time, showing rooms,
guest messages, engine decisions, and approval requests on the manager's phone.
Occupancy and readings come from the dataset. The architecture, characters, and
their activities are illustrative, not measured coordinates or human behavior. All
40 source rooms appear on one level with reception, a waiting area, and a housekeeping
room. This is a convenient overview layout; source IDs and floor numbers in the data
are unchanged. The restored scene uses low cutaway walls, 20 cm thick, so room
activities remain visible. The roof is open for viewing. On a wide screen, the
hotel is on the left and the manager's phone is on the right. Use **Requests** on
the phone to inspect guest messages. Actions remain simulated.

## Current interface snapshot and provenance

The current `hotel_web` interface restores the reviewed 3D hotel view, sidebar,
manager phone, and theme from `outputs/release-candidate/incoming/hotel-current-3d-review.zip`
(SHA-256 `43b737cf1937f652835862e09b044d4317296d3c6a09e7c6527eda22c54f1965`).
The source was based on snapshot `f88f3fb` with local fixes and English interface
strings. The preview at `http://127.0.0.1:8765` was visually checked; the isolated
frontend passed 68 JavaScript tests and its build succeeded.

This UI restoration and the English-string updates were made after the frozen
`bff983d` Holdout runtime. They are presentation changes and are not part of that
runtime or its completed primary Cerebras Holdout job. The earlier offline
diagnostic is retained separately and is not the primary API submission. The
completed run contains 65,691 events, 1,534 guest messages and 8,986 decisions;
both validators report zero errors. See [run provenance](reviews/holdout_run.json)
and [validation](reviews/holdout_validation.json).

[Open the saved Holdout](http://127.0.0.1:8765/?replay=replay-3205dc5524b1474b9c54386d97afa4f5)
and use **Play**. It is already prepared; **Prepare again** would start another
model/engine run. Playback was checked with advancing time, occupancy and
decisions, followed by Pause.

## Run

From the repository root:

```bash
python -m pip install -e '.[dev]'
python -m hotel_bot
```

Open [Hotel](http://127.0.0.1:8765). On a Mac, you can use `Start Hotel.command`.
To use another port, pass `--port 8766`. The site is available only on this computer.
The Cerebras key is stored outside the project at `~/.config/hotel/cerebras.key` or
provided through `CEREBRAS_API_KEY`. It is not included in the browser build.

## Show the full dataset

1. Select Quickstart or Dev. If a saved full replay is available for that dataset,
   the site opens it from the beginning in a paused state.
2. Click **Play**. The clock follows historical time; events, messages, and decisions
   appear as their availability times are reached.
3. **3-minute tour** compresses the entire period into three minutes: early bookings
   before operations begin pass in 5 seconds, and the remaining events play over
   175 seconds. This avoids a long empty start; no records are removed. At other
   speeds, you can inspect a day or a specific case in more detail. Faster playback
   does not remove events: counters include the entire stream prefix that has played.
4. **Pause** stops the clock. The slider moves through the history, and **Restart**
   returns to the beginning. Seeking backward removes future messages, decisions,
   approvals, and log entries. Decisions at the same time retain their source order;
   observations use the same time-availability boundary as the original engine.
5. **Overview** shows the whole hotel; **Reception** and **Housekeeping** zoom in on
   shared areas. Selecting a room zooms in on it; rotate the hotel with the mouse,
   pan with the right mouse button, and zoom with the wheel. All 40 rooms remain in
   the list. Residents appear only in rooms with confirmed occupancy. They may read,
   rest, and move around as an illustration of hotel life.
6. Expand **Action log** to see each action, time, and reason. **Download log** saves
   all entries up to the selected moment. At the end of playback, it is the full log.
   Complete run artifacts are also available separately in the collapsed downloads.

The full reference Quickstart contains 3,939 normalized events and 112 guest
messages; Dev contains 65,434 events and 1,612 messages. Telemetry, bookings, and
other source tables are queried through the gateway at each availability time. The
number of events in the stream is not the sum of all rows in all source CSV files.

**Play replays the saved completed run.** It does not send messages to the model again
or start a second engine. This makes the demo reproducible without waiting for network
responses. Preparing a new dataset is a separate action; its progress and status are
shown separately from the presentation clock. Preparation uses Cerebras or a suitable
verified cache, then the actual `DecisionEngine`.

Playback counters are not model-accuracy measurements. Source labels in `request_type`
are preserved in the production context; they do not measure independent semantic
accuracy. Preparation errors and fallback results are marked explicitly.

## Short presentation walkthrough

Start with Quickstart and click **Play**. Show the moving clock and changing occupancy,
then zoom in on a room or shared area. When a message appears, pause and compare the
guest request, decision, and reason in the log. Routine tasks are created autonomously.
Only actual approval requirements from the engine appear on the phone; these are
historical records and cannot be approved retroactively.

Seek backward to show a future request disappearing. Then return to the end and
download the log. For a longer demonstration, switch to Dev: the same controls cover
the full year of data without tailoring the scenario to a particular room.

## Show planning without future data leakage

The official scoring table has no separate “predicting the future — 20 points”
criterion. The 20 points are for the demo and presentation: scenarios and visualization
are worth 8, the decision log 7, and limitations and verification 5. Forward planning
also relates to autonomy, energy, logistics, and recommendations.

Every decision has a timestamp. Measurements later than that time are hidden. A weather
forecast may refer to the future, but its `issued_utc` must already have arrived; likewise,
a tariff is available only after `published_utc`. A booking is visible after it is
created, and a staff absence only after publication. Annual analysis remains
retrospective and is not fed into past decisions. Preparing the full ZIP in advance
does not bypass these limits: **Prepare dataset** processes events sequentially through
the gateway as of each event time.

Verified examples from the saved reference Dev replay
`replay-6048e67101754c4fa2f321224263a6ae` (Europe/Vienna local time):

1. **January 2, 13:00, room 304.** Preheat a vacant room to 21 °C before a known
   upcoming arrival. The plan uses a 14:00 forecast issued at 06:00 and a tariff that
   has already been published. This uses an available external forecast, not an
   in-house weather model. Decision: `decision:01e7737429889a36d4dc`.
2. **January 3, 15:00, central waste container.** At 59.6% full, the accumulation
   rate over the preceding hours estimates that it will reach capacity in about
   11.6 hours. The next scheduled pickup is later, so the engine proposes an extra
   service. This is a linear extrapolation from past observations. The assumed
   24-hour service lead time means the order alone does not remove the near-term risk.
   Decision: `decision:48370719b2ae173b9106`.
3. **January 2, 00:00, battery.** The currently known price of €0.213/kWh is compared
   with the published range of €0.188–0.293. The system proposes a limited 15-minute
   charge, accounting for losses and the battery's permitted charge level. This is
   heuristic planning with known prices; savings have not been demonstrated.
   Decision: `decision:b44a1a9ee19e8b151a9c`.

To show an example, open Dev, pause at the relevant time, and expand **Action log**.
Compare the decision time, **Why**, source observation references, and **Verify**.
For an exact lookup, use the full downloadable log and the decision ID. The current
interface has no separate forecast chart or jump-to-time control. Present the chain
“what is known now → risk estimate → decision → how the result will be checked.” A
sensor reading from the future is not evidence for a past decision.

Do not claim an in-house trained weather predictor, a prediction of tomorrow's leak,
proven savings, or 100% accuracy. Water incidents are detected from consumption
already observed; forecasts of shortages and overflow in logistics are calculated
separately.

## Import a new dataset from ZIP

Click **Upload ZIP** and select an archive. The app finds a Hotel file set inside
nested folders, checks its structure, and adds it to the dataset list. Import happens
locally and does not contact Cerebras. To process the selected new dataset, click
**Prepare dataset** separately and wait for it to finish.

After preparation, **Play** can be used again without rerunning the analysis or making
new model requests. The prepared archive is retained after the site restarts. Uploading
the same data again is recognized and should not require preparation again.

The app supports the 17-file PLATO40 set for rooms 101–110, 201–210, 301–310, and
401–410; it does not accept an arbitrary ZIP containing any table. If an archive is
corrupt, encrypted, contains multiple complete sets, or is missing required files,
the site explains why. Limits: 128 MiB for the ZIP and 512 MiB for extracted data.
Import does not overwrite the original Quickstart or Dev data; imported data is stored
separately in `outputs/imported-datasets/`.

## Submit a custom request through the API

Switch to interactive mode and send your own message. The rooms and their states here
are a separate demonstration example; the source dataset is unchanged. Options under
**Try a request**:

- Towels or Wi-Fi: an autonomous administrative task for the relevant service.
- Early check-in: the existing turnaround planner checks rooms and resources.
- Inspect a fault in an occupied room: manager approval and separate guest consent
  are required. Manager approval alone does not authorize entry.
- An unclear request: ask for clarification without inventing an action or adding an
  unnecessary approval step.

The model extracts intent, and the engine determines actions. Every candidate passes
through `validate_safety` and `MockExecutor`. A queued task does not mean that a worker
was actually dispatched, a door was opened, a message was delivered, or a repair was
completed. `effects_applied=false`; historical observations are not changed.

Repeating a request with the same ID does not repeat model extraction or the action;
different text with the same ID is rejected. An interactive session accepts up to
200 new messages. Restarting the server begins a new interactive session; logs are
retained.

## Saved data and validation

Quickstart and Dev are independent and are never merged. Inputs are stored in
`data/PLATO40_v2_quickstart` and `data/PLATO40_v2_dev` and are not overwritten.
Complete results are stored in `outputs/bot-replays/<run-id>/`, including
`extractions.jsonl` and `rejections.jsonl`. The summary stores SHA-256 hashes of the
inputs. When a replay is restored, integrity and source consistency are checked;
changed data is not silently substituted into the old history.

Interactive session logs are stored in
`outputs/bot-session/<session-id>/actions.jsonl`. Each entry includes the action, its
explanation, and the actual simulation status. Technical fields remain in the
downloadable log rather than the main screen. The original CLI `run.py` and the
independent validator are still available.
