/** Read scheduling metadata without repeatedly downloading an unchanged publication. */
export function mountCaptureRefresh({ layer, kind, request, refresh, signal }) {
  let controller, timer, captureRevision, publicationRevision, scheduleKey, nextFetchAt = 0, attempts = 0, stopped = false;
  function stop() { clearTimeout(timer); controller?.abort(); controller = undefined; }
  async function check(turn) {
    let delay;
    try {
      const status = await request(`/api/prisma/capture-status?kind=${kind}`, {
        signal: AbortSignal.any([turn.signal, AbortSignal.timeout(15000)]),
      });
      if (turn.signal.aborted || controller !== turn) return;
      const receivedAt = performance.now();
      const now = Date.parse(status.server_now), next = status.next_capture_at === null ? null : Date.parse(status.next_capture_at);
      const interval = status.schedule?.interval_minutes;
      if (!Number.isFinite(now) || (next !== null && !Number.isFinite(next)) || !Number.isInteger(interval) || interval < 1 || interval > 1440
        || typeof status.processing_pending !== 'boolean' || typeof status.capture_revision !== 'string'
        || ![status.server_now, status.next_capture_at].every(value => value === null || /(?:Z|[+-]\d\d:\d\d)$/.test(value))
        || ![status.publication_revision, status.publication_version].every(value => value === null || (typeof value === 'string' && value.length > 0))
        || (status.publication_revision !== null && status.publication_version === null)) {
        throw new Error('Invalid capture schedule status.');
      }
      const configuration = JSON.stringify([status.schedule.start_at, interval, status.schedule.config_version]);
      if (configuration !== scheduleKey) { scheduleKey = configuration; nextFetchAt = 0; }
      if (captureRevision !== status.capture_revision) { captureRevision = status.capture_revision; attempts = 0; }
      const current = layer.state(), revision = status.publication_revision;
      const first = publicationRevision === undefined;
      const applied = current.snapshot[kind === 'social' ? 'social_revision' : 'sensor_revision']
        || (first && status.publication_version === current.snapshot.version ? revision : publicationRevision);
      const changed = revision !== null && revision !== applied;
      const intervalMs = interval * 60000, anchor = Date.parse(status.schedule.start_at);
      const nextSlot = Number.isFinite(anchor) ? anchor + Math.max(0, Math.floor((now - anchor) / intervalMs) + 1) * intervalMs : now + intervalMs;
      const nextWindow = next === null ? now + intervalMs : next > now ? next : nextSlot;
      let waiting = false;
      if (changed && now >= nextFetchAt) {
        waiting = current.isRefreshing || !await refresh(layer.id, { signal: turn.signal });
        if (!waiting && !turn.signal.aborted) { publicationRevision = revision; nextFetchAt = nextWindow; }
      } else if (!changed && revision !== null) {
        publicationRevision = revision;
        if (first) nextFetchAt = nextWindow;
      }
      const elapsed = performance.now() - receivedAt, cadence = Math.min(intervalMs, 300000);
      if (now < nextFetchAt) {
        attempts = 0; delay = next === null ? nextFetchAt - now - elapsed : Math.min(nextFetchAt - now - elapsed, cadence);
      } else if (next === null) {
        attempts = 0; delay = intervalMs;
      } else if (waiting || status.processing_pending || next <= now || nextFetchAt > 0) {
        // ponytail: four quick checks, then bounded metadata polling; an unrelated layer's publication never resets this budget.
        delay = [5000, 10000, 20000, 30000][attempts++] ?? Math.max(30000, cadence);
      } else {
        attempts = 0;
        delay = Math.min(next - now - elapsed, cadence);
      }
    } catch {
      delay = Math.min(5000 * 2 ** Math.min(attempts++, 6), 300000);
    } finally {
      if (!turn.signal.aborted && controller === turn && layer.state().enabled) timer = setTimeout(() => void check(turn), Math.max(1000, delay));
    }
  }
  const unsubscribe = layer.subscribe(({ enabled }) => {
    if (!enabled) stop();
    else if (!controller && !stopped) {
      controller = new AbortController(); attempts = 0; publicationRevision = undefined; nextFetchAt = 0;
      const turn = controller; timer = setTimeout(() => void check(turn), 0);
    }
  });
  const dispose = () => { stopped = true; stop(); unsubscribe(); signal?.removeEventListener('abort', dispose); };
  if (signal?.aborted) dispose(); else signal?.addEventListener('abort', dispose, { once: true });
  return dispose;
}
