"""Indexed lookups must equal the previous scan algorithms, including boundaries."""
from bisect import bisect_left, bisect_right
import csv
from datetime import datetime, timedelta, timezone
import json
import random

import pytest

from ingestion.loader import Series, norm_ts
from ingestion.replay import ReplayData

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def stamp(hours=0, seconds=0):
    return (BASE+timedelta(hours=hours, seconds=seconds)).isoformat().replace("+00:00", "Z")


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def indexed_repo(tmp_path):
    (tmp_path/"building.json").write_text('{"time":{"local_timezone":"Europe/Vienna"}}')
    rng = random.Random(143)
    tariff_rows = [{"timestamp_utc": stamp(i), "published_utc": stamp(i-24, seconds=(i%4)*900) if i%17 else "",
                    "price_eur_kwh": "" if i%31 == 0 else (i%13-4)/100} for i in range(300)]
    # Dataset.tariffs intentionally retains the first duplicate target, not a later correction.
    tariff_rows.insert(3, {"timestamp_utc": stamp(0), "published_utc": stamp(-50), "price_eur_kwh": 999})
    rng.shuffle(tariff_rows)
    write_csv(tmp_path/"tariff.csv", ["timestamp_utc", "published_utc", "price_eur_kwh"], tariff_rows)
    forecasts = [{"target_utc": stamp(i), "issued_utc": stamp(i+offset), "outdoor_c_forecast": (i+offset)%19-8}
                 for i in range(300) for offset in [-48, -24, -6, 1]]
    forecasts += [{"target_utc": stamp(20), "issued_utc": stamp(14), "outdoor_c_forecast": 999},
                  {"target_utc": stamp(30), "issued_utc": "", "outdoor_c_forecast": -999},
                  {"target_utc": "", "issued_utc": stamp(10), "outdoor_c_forecast": -999}]
    rng.shuffle(forecasts)
    write_csv(tmp_path/"weather_forecast.csv", ["issued_utc", "target_utc", "outdoor_c_forecast"], forecasts)
    return ReplayData(tmp_path, use_llm=False)


def old_tariffs(repo, as_of_utc, until_utc):
    as_of, until = norm_ts(as_of_utc), norm_ts(until_utc)
    first = as_of[:13]+":00:00Z"
    return [dict(r) for r in repo.ds.tariffs if first <= r["timestamp_utc"] <= until
            and r.get("published_utc") is not None and r["published_utc"] <= as_of]


def old_tariff_measurements(repo, start_utc, end_utc, as_of_utc):
    start, as_of = norm_ts(start_utc), norm_ts(as_of_utc)
    end = min(norm_ts(end_utc), as_of)
    return [dict(r) for r in repo.ds.tariffs if start <= r["timestamp_utc"] <= end
            and r.get("published_utc") is not None and r["published_utc"] <= as_of]


def old_forecasts(repo, as_of_utc, until_utc):
    as_of, until = norm_ts(as_of_utc), norm_ts(until_utc)
    first = as_of[:13]+":00:00Z"
    out = []
    for target, rows in repo.ds.forecasts.items():
        if target < first:
            continue
        if target > until:
            break
        known = [r for r in rows if r["issued_utc"] <= as_of]
        if known:
            out.append(dict(known[-1]))
    return out


def old_keys(series, entity_id, metric):
    return sorted(k for k in series.groups
                  if (entity_id is None or not series.entity_col or k[0] == entity_id)
                  and (metric is None or not series.metric_col or k[1] == metric))


def old_query(series, start, end, entity_id, metric):
    if end < start:
        return []
    picked = []
    for order, key in enumerate(old_keys(series, entity_id, metric)):
        times, lines = series.groups[key]
        lo, hi = bisect_left(times, start), bisect_right(times, end)
        picked += [(times[i], order, lines[i]) for i in range(lo, hi)]
    picked.sort(key=lambda item: (item[0], item[1]))
    return [series._parse_line(item[2]) for item in picked]


def old_latest(series, as_of, entity_id, metric):
    best = None
    for key in old_keys(series, entity_id, metric):
        times, lines = series.groups[key]
        i = bisect_right(times, as_of)
        if i and (best is None or times[i-1] > best[0]):
            best = (times[i-1], lines[i-1])
    return series._parse_line(best[1]) if best else None


def test_tariffs_and_forecasts_match_old_scans_across_random_windows(indexed_repo):
    repo, rng = indexed_repo, random.Random(492)
    for _ in range(200):
        hour = rng.randint(-60, 340)
        now = stamp(hour, seconds=rng.choice([0, 1, 899, 3599]))
        until = stamp(hour+rng.randint(-10, 60))
        start = stamp(hour-rng.randint(-10, 60))
        assert repo.get_tariffs(now, until) == old_tariffs(repo, now, until)
        assert repo.get_weather_forecast(now, until) == old_forecasts(repo, now, until)
        assert repo.get_measurements("tariff.csv", start, until, now) == old_tariff_measurements(repo, start, until, now)


def test_publication_issue_and_target_boundaries_remain_inclusive(indexed_repo):
    repo = indexed_repo
    for row in repo.ds.tariffs[1:70]:
        if row["published_utc"]:
            boundary = datetime.fromisoformat(row["published_utc"].replace("Z", "+00:00"))
            for seconds in [-1, 0, 1]:
                now = (boundary+timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
                assert repo.get_tariffs(now, row["timestamp_utc"]) == old_tariffs(repo, now, row["timestamp_utc"])
    for target, entries in list(repo.ds.forecasts.items())[:30]:
        for row in entries:
            boundary = datetime.fromisoformat(row["issued_utc"].replace("Z", "+00:00"))
            for seconds in [-1, 0, 1]:
                now = (boundary+timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
                assert repo.get_weather_forecast(now, target) == old_forecasts(repo, now, target)


def test_equivalent_time_formats_and_result_mutation_do_not_change_indexes(indexed_repo):
    repo = indexed_repo
    now, until = "2026-01-04T02:30:00+01:00", stamp(90)
    expected_prices = old_tariffs(repo, now, until)
    expected_forecasts = old_forecasts(repo, now, until)
    prices, forecasts = repo.get_tariffs(now, until), repo.get_weather_forecast(now, until)
    assert prices == expected_prices and forecasts == expected_forecasts
    prices[0]["price_eur_kwh"] = 9999
    forecasts[0]["outdoor_c_forecast"] = 9999
    assert repo.get_tariffs(now, until) == expected_prices
    assert repo.get_weather_forecast(now, until) == expected_forecasts
    # Tariff history has never filtered entity/metric: preserve that public behavior.
    assert repo.get_measurements("tariff.csv", stamp(0), until, now, "unknown", "unknown") == old_tariff_measurements(repo, stamp(0), until, now)


@pytest.mark.parametrize("filename,fields,entries", [
    ("equipment.csv", ["timestamp_utc", "asset_id", "metric", "value"],
     [{"timestamp_utc": stamp(i%3), "asset_id": asset, "metric": metric, "value": i}
      for i in range(7) for asset in ["HP1", "AHU1", ""] for metric in ["status", "electric_kw", ""]]),
    ("room_telemetry.csv", ["timestamp_utc", "room_id", "temperature_c"],
     [{"timestamp_utc": stamp(i%3), "room_id": room, "temperature_c": i} for i in range(7) for room in ["R101", "R102", ""]]),
    ("energy.csv", ["timestamp_utc", "building_load_kw"],
     [{"timestamp_utc": stamp(i%3), "building_load_kw": i} for i in range(7)]),
    ("access_log.csv", ["timestamp_utc", "door_id", "credential", "result"],
     [{"timestamp_utc": stamp(i%3), "door_id": door, "credential": "B"+str(i), "result": "granted"}
      for i in range(7) for door in ["R101", "MAIN_ENTRANCE", ""]]),
])
def test_series_key_indexes_preserve_filtering_tie_order_query_and_latest(tmp_path, filename, fields, entries):
    write_csv(tmp_path/filename, fields, entries)
    series = Series(tmp_path/filename, filename)
    entities = [None, "", "unknown", "HP1", "AHU1", "R101", "MAIN_ENTRANCE"]
    metrics = [None, "", "unknown", "status", "electric_kw"]
    for entity in entities:
        for metric in metrics:
            expected = old_keys(series, entity, metric)
            actual = series._keys(entity, metric)
            assert actual == expected
            actual.clear()  # returned key lists must not mutate precomputed indexes
            assert series._keys(entity, metric) == expected
            for a, z in [(stamp(-1), stamp(4)), (stamp(1), stamp(1)), (stamp(2), stamp(0))]:
                assert series.query(a, z, entity, metric) == old_query(series, a, z, entity, metric)
            for now in [stamp(-1), stamp(0), stamp(1), stamp(9)]:
                assert series.latest(now, entity, metric) == old_latest(series, now, entity, metric)


def test_empty_indexes_preserve_empty_results(tmp_path):
    (tmp_path/"building.json").write_text('{}')
    write_csv(tmp_path/"tariff.csv", ["timestamp_utc", "published_utc", "price_eur_kwh"], [])
    write_csv(tmp_path/"weather_forecast.csv", ["issued_utc", "target_utc", "outdoor_c_forecast"], [])
    write_csv(tmp_path/"energy.csv", ["timestamp_utc", "building_load_kw"], [])
    repo = ReplayData(tmp_path, use_llm=False)
    assert repo.get_tariffs(stamp(), stamp(10)) == []
    assert repo.get_weather_forecast(stamp(), stamp(10)) == []
    assert repo.get_measurements("tariff.csv", stamp(), stamp(10), stamp(10)) == []
    series = Series(tmp_path/"energy.csv", "energy.csv")
    assert series._keys(None, None) == [] and series.latest(stamp()) is None
