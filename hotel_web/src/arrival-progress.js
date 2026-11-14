/** Arrival timing uses replay timestamps, so pauses and speed changes are exact. */
export function arrivalProgress(startedAt, currentTime, routeLength) {
  if (![startedAt, currentTime, routeLength].every(Number.isFinite) || routeLength < 0) {
    return {moving:false, fraction:0, elapsed:0};
  }
  const elapsed = Math.max(0, currentTime - startedAt);
  const duration = Math.max(5000, routeLength / 2.5 * 1000);
  return {elapsed, moving:elapsed >= 1600, fraction:Math.min(1, Math.max(0, (elapsed - 1600) / duration))};
}
