"""Hourly waste/vending forecasts and feasible robot bin transfers."""
from collections import defaultdict
from copy import deepcopy
from datetime import timedelta

from engine.core import Candidate, DetectionResult, VIENNA, ago, instant, number, observations, utc


def next_regular(now, weekdays, hour):
    local = instant(now).astimezone(VIENNA)
    for offset in range(8):
        day = local + timedelta(days=offset)
        candidate = day.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate.weekday() in weekdays and candidate > local:
            return utc(candidate)
    raise ValueError("No regular service day")


class LogisticsDetector:
    def __init__(self, config):
        self.config = config
        self.open_transfers = {}
        self.destination_commitments = {}

    def detect(self, event, repo, building, calendar):
        result = DetectionResult()
        # Cancellation is already an observed planner fact. Retire its marker
        # even on roster events, without proposing new physical work.
        cancelled = {entry["decision_id"] for entry in calendar.cancellations}
        for asset, held in list(self.open_transfers.items()):
            if held["decision_id"] in cancelled:
                self.open_transfers.pop(asset)
                self.destination_commitments.pop(held["decision_id"], None)
                result.resolved.add("logistics:"+asset)
        for ident in cancelled:
            self.destination_commitments.pop(ident, None)
        if event.event_type != "clock_tick":
            return result
        now = event.available_at_utc
        assets = {a["id"]: a for a in building["building"].get("assets", [])}
        groups = defaultdict(list)
        for row in observations(repo, "logistics.csv", ago(now, 12*60), now):
            groups[(row.get("asset_id"), row.get("metric"))].append(row)
        fresh = {key: rows[-1] for key, rows in groups.items()
                 if instant(rows[-1]["timestamp_utc"]) >= instant(ago(now, 60))}
        events = [e for e in repo.get_events(ago(now, 12*60), now, now)
                  if e.get("timestamp_utc") and instant(ago(now, 12*60)) <= instant(e["timestamp_utc"]) <= instant(now)
                  and (not e.get("available_at_utc") or instant(e["available_at_utc"]) <= instant(now))]
        observed_updates = {}
        for e in events:
            stamp, asset = e["timestamp_utc"], e.get("asset_id")
            if not asset or e.get("type") not in {"waste_pickup", "floor_bin_emptied", "vending_restocked"}:
                continue
            if asset not in observed_updates or instant(stamp) > instant(observed_updates[asset]):
                observed_updates[asset] = stamp
        main = fresh.get(("WASTE_MAIN", "fill_pct"))
        if main and observed_updates.get("WASTE_MAIN") and instant(observed_updates["WASTE_MAIN"]) > instant(main["timestamp_utc"]):
            main = None  # a pre-pickup level cannot establish post-pickup capacity
        self._reconcile_transfers(result, calendar, assets, fresh, main, events, observed_updates, now)
        # Existing commitments are checked each clock tick; new plans/forecasts
        # keep their original hourly cadence.
        if instant(now).minute != 0:
            return result
        planning = deepcopy(calendar)
        for (asset, metric), latest in sorted(fresh.items()):
            value = number(latest.get("value"))
            if value is None or asset not in assets:
                continue
            evidence = [f"logistics.csv:{asset}:{metric}:{latest['timestamp_utc']}"]
            if asset in observed_updates and instant(observed_updates[asset]) > instant(latest["timestamp_utc"]):
                # A known pickup/restock/emptying after this reading makes its
                # level obsolete; do not redispatch or order from a stale level.
                continue
            if metric == "fill_pct":
                if not 0 <= value <= 100:
                    continue
                if value < 40:
                    result.resolved.add("logistics:" + asset)
                    if asset == "WASTE_MAIN":
                        result.resolved.add("service:WASTE_MAIN:waste_pickup")
                    continue
                if asset.startswith("WASTE_F") and value >= self.config.waste_trigger_pct:
                    if asset in self.open_transfers:
                        held = self.open_transfers[asset]
                        if instant(now) >= instant(held["end_utc"]) + timedelta(hours=1):
                            result.candidates.append(Candidate("logistics:"+asset, asset, "logistics", "flag_anomaly",
                                "A simulated bin task has no observed level recovery; verify it instead of assuming the bin was emptied or repeating the command.",
                                evidence, "Check robot acknowledgement, source level and route before replanning.",
                                "Flag an unverified simulated task.", variant="unverified_task"))
                        continue
                    destination = assets.get("WASTE_MAIN", {})
                    destination_pct = number(main.get("value")) if main else None
                    capacity = number(destination.get("volume_l"))
                    volume = number(assets[asset].get("volume_l"))
                    transfer = volume * value / 100 if volume else None
                    can_hold = (main and destination_pct is not None and 0 <= destination_pct <= 100 and capacity
                                and transfer and planning.capacity_available("WASTE_MAIN", capacity*destination_pct/100,
                                        capacity*self.config.container_max_fill_pct/100, transfer))
                    slots = repo.get_staff_slots(now, utc(instant(now)+timedelta(hours=2)), now, role="robot")
                    choice = planning.first_slot(slots, now, utc(instant(now)+timedelta(hours=2)), self.config.robot_minutes, role="robot") if can_hold else None
                    if choice:
                        start, end, robot, slot = choice
                        planning.reserve(robot, start, end, "planned:"+asset, now)
                        planning.reserve_volume("WASTE_MAIN", transfer, "planned:"+asset)
                        context = {"task": "carry_bin", "common_area_route": True, "slot": slot,
                                   "start_utc": start, "end_utc": end, "source_bin": assets[asset],
                                   "source_observation": latest, "destination_bin": destination,
                                   "destination_observation": main, "transfer_liters": transfer,
                                   "container_max_fill_pct": self.config.container_max_fill_pct}
                        result.candidates.append(Candidate("logistics:"+asset, asset, "logistics", "dispatch_robot",
                            f"Floor bin {value:.1f}% full. Propose {robot} transfer of approximately {transfer:.1f} L {start}..{end}; central capacity and existing planned transfers fit. Bin transport only, not room cleaning or measured emptying.",
                            evidence+[f"logistics.csv:WASTE_MAIN:{main['timestamp_utc']}", f"staff_roster.csv:{slot['shift_id']}"],
                            "Observe the source bin drop and destination level/pickup; otherwise investigate the unverified task.",
                            "Reserve a common-area bin transport and destination capacity separately from observed telemetry.", resource=robot, context=context))
                    else:
                        result.candidates.append(Candidate("logistics:"+asset, asset, "logistics", "flag_anomaly",
                            f"Floor bin {value:.1f}% full; no confirmed robot slot or safe central-container capacity. Do not invent a collection or move waste into a full container.",
                            evidence, "Confirm robot availability and destination capacity; seek human handling if the bin remains full.",
                            "Raise a logistics coordination request.", priority="high" if value >= 100 else "medium", variant="blocked"))
                elif asset == "WASTE_MAIN":
                    rows = self._post_update(groups[(asset, metric)], observed_updates.get(asset))
                    rate = self._rate(rows, increasing=True)
                    hours = (100-value)/rate if rate and rate > 0 else None
                    if value >= self.config.waste_trigger_pct or (hours is not None and hours < 24):
                        if hours is not None:
                            evidence.append(f"logistics.csv:{asset}:{metric}:{rows[0]['timestamp_utc']}..{latest['timestamp_utc']} (trend input window)")
                        regular = next_regular(now, {1, 4}, 7)
                        self._service(result, asset, "waste_pickup", value, hours, regular, now, evidence)
            elif metric in {"stock_drinks", "stock_snacks"}:
                capacity = number(assets[asset].get("capacity", {}).get(metric.removeprefix("stock_")))
                if not capacity or not 0 <= value <= capacity:
                    continue
                history = self._post_update(groups[(asset, metric)], observed_updates.get(asset))
                rate = self._rate(history, increasing=False, max_value=capacity)
                hours = value/rate if rate and rate > 0 else None
                if value <= capacity*self.config.vending_low_fraction or (hours is not None and hours < 24):
                    if hours is not None:
                        evidence.append(f"logistics.csv:{asset}:{metric}:{history[0]['timestamp_utc']}..{latest['timestamp_utc']} (trend input window)")
                    regular = next_regular(now, {0, 3}, 10)
                    self._service(result, asset, metric, value, hours, regular, now, evidence)
                elif value > capacity*0.5:
                    result.resolved.add("service:"+asset+":"+metric)
        return result

    @staticmethod
    def _post_update(rows, update):
        # A known restock/pickup is a discontinuity even when subsequent sampled
        # levels happen to continue the old monotone trend.
        return [row for row in rows if not update or instant(row["timestamp_utc"]) > instant(update)]

    def _cancel_transfer(self, result, calendar, asset, held, reason, evidence):
        count = len(calendar.cancellations)
        calendar.cancel(held["decision_id"], reason=reason)
        for entry in calendar.cancellations[count:]:
            if entry["decision_id"] == held["decision_id"]:
                entry["evidence"] = list(evidence)
        self.open_transfers.pop(asset, None)
        self.destination_commitments.pop(held["decision_id"], None)
        result.resolved.add("logistics:"+asset)

    def _reconcile_transfers(self, result, calendar, assets, fresh, main, events, updates, now):
        starts = {ident: start for _, jobs in calendar.iter_reservations() for start, end, ident in jobs}
        for asset, held in list(self.open_transfers.items()):
            ident = held["decision_id"]
            commitment = self.destination_commitments.setdefault(ident, {**held, "source_asset": asset})
            if ident in starts:
                commitment["start_utc"] = starts[ident]
            row = fresh.get((asset, "fill_pct"))
            value = number(row.get("value")) if row else None
            source_capacity = number(assets.get(asset, {}).get("volume_l"))
            reserved = dict(calendar.volume_reservations.get("WASTE_MAIN", [])).get(ident)
            if (commitment.get("start_utc") and instant(now) <= instant(commitment["start_utc"])
                    and value is not None and 40 <= value <= 100 and source_capacity and source_capacity > 0
                    and reserved is not None and abs(source_capacity*value/100-reserved) > 1e-8):
                # A carry-bin command transports the whole observed source.
                # Replacing its immutable amount under the old ID would hide
                # new capacity requirements, even if today's headroom fits.
                self._cancel_transfer(result, calendar, asset, held,
                    f"Fresh source level at {row['timestamp_utc']} changes the whole-bin volume from {reserved:.6g} L to {source_capacity*value/100:.6g} L; withdraw the immutable future command and replan with a new ID",
                    [f"logistics.csv:{asset}:fill_pct:{row['timestamp_utc']}"])
                continue
            recovery = row["timestamp_utc"] if value is not None and 0 <= value < 40 else None
            if updates.get(asset) and (not row or instant(updates[asset]) > instant(row["timestamp_utc"])):
                recovery = updates[asset]
            if recovery:
                if commitment.get("start_utc") and instant(now) <= instant(commitment["start_utc"]):
                    refs = ([f"logistics.csv:{asset}:fill_pct:{recovery}"]
                            if row and recovery == row["timestamp_utc"] else
                            [f"events.jsonl:{e.get('event_id') or asset}:{recovery}" for e in events
                             if e.get("asset_id") == asset and e["timestamp_utc"] == recovery])
                    self._cancel_transfer(result, calendar, asset, held,
                        f"Observed source recovery at {recovery} makes the future bin transfer unnecessary", refs)
                else:
                    # Source recovery does not say where waste went. In
                    # particular it cannot free an unobserved destination.
                    commitment["source_recovered_utc"] = recovery
                    self.open_transfers.pop(asset)
                    result.resolved.add("logistics:"+asset)
        pickups = [e["timestamp_utc"] for e in events
                   if e.get("type") == "waste_pickup" and e.get("asset_id") == "WASTE_MAIN"]
        main_value = number(main.get("value")) if main else None
        valid_main = main is not None and main_value is not None and 0 <= main_value <= 100
        for ident, held in list(self.destination_commitments.items()):
            recovered = held.get("source_recovered_utc")
            post_recovery_main = (valid_main and recovered
                and instant(main["timestamp_utc"]) >= max(instant(held["end_utc"]), instant(recovered)))
            matching_pickup = any(instant(stamp) >= instant(held["end_utc"]) for stamp in pickups)
            if post_recovery_main or matching_pickup:
                calendar.release_volume(ident)
                self.destination_commitments.pop(ident)
        capacity = number(assets.get("WASTE_MAIN", {}).get("volume_l"))
        limit = capacity*self.config.container_max_fill_pct/100 if capacity else None
        # Withdraw the latest future jobs first if new observations invalidate
        # their shared capacity. Elapsed work remains uncertain, not cancelled.
        for ident, held in sorted(list(self.destination_commitments.items()),
                                  key=lambda item: (item[1].get("start_utc", ""), item[0]), reverse=True):
            if not held.get("start_utc") or instant(held["start_utc"]) < instant(now):
                continue
            total = sum(amount for _, amount in calendar.volume_reservations.get("WASTE_MAIN", []))
            if valid_main and limit and capacity*main_value/100+total > limit+1e-8:
                refs = [f"logistics.csv:WASTE_MAIN:fill_pct:{main['timestamp_utc']}"]
                self._cancel_transfer(result, calendar, held["source_asset"], held,
                    f"Latest destination evidence as of {now} does not verify capacity for the future transfer; withdraw its proposed resource and volume hold", refs)

    @staticmethod
    def _rate(rows, increasing, max_value=100):
        # Use the monotonic segment since the last observed pickup/restock/reset.
        segment = []
        for row in rows:
            value = number(row.get("value"))
            if value is None or not 0 <= value <= max_value:
                segment = []
                continue
            if segment:
                old = number(segment[-1].get("value"))
                gap = (instant(row["timestamp_utc"])-instant(segment[-1]["timestamp_utc"])).total_seconds()/3600
                if gap > 2 or gap <= 0 or (value < old if increasing else value > old):
                    segment = []
            segment.append(row)
        if len(segment) < 3:
            return None
        hours = (instant(segment[-1]["timestamp_utc"])-instant(segment[0]["timestamp_utc"])).total_seconds()/3600
        change = float(segment[-1]["value"])-float(segment[0]["value"])
        return (change if increasing else -change)/hours if hours > 0 else None

    def _service(self, result, asset, kind, value, hours, regular, now, evidence):
        key = "service:"+asset+":"+kind
        regular_hours = (instant(regular)-instant(now)).total_seconds()/3600
        # A regular visit that precedes the forecast shortage beats a new 24h order.
        can_wait = hours is not None and hours > regular_hours + 1
        if value == 0 and kind.startswith("stock_"):
            can_wait = False
        action = "defer_action" if can_wait else "order_service"
        estimate = f"trend implies limit in ~{hours:.1f} h" if hours is not None else "depletion/overflow time is uncertain"
        result.candidates.append(Candidate(key, asset, "logistics", action,
            f"{kind}: observed {value:.1f}; {estimate}. Next regular service {regular}. " +
            ("Monitor the existing regular service before spending on an extra call." if can_wait else
             "Propose extra service; ~24 h lead time is an assumption and vendor/cost must be confirmed. This does not solve an immediate shortage."),
            evidence, "Verify a real pickup/restock and level recovery; escalate if a proposed or regular service is not observed.",
            "Plan a service request; never assert vendor acceptance, pickup or delivery.",
            human=not can_wait, priority="high" if value >= 100 and kind=="waste_pickup" or value == 0 and kind.startswith("stock_") else "medium",
            context={"service": kind, "lead_hours": 24, "notable_cost": not can_wait}, variant=action))
