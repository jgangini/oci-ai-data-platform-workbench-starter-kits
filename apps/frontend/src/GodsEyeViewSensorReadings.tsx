import { LoadingIndicator } from './LoadingIndicator';
import { useEffect, useState, type ReactNode } from 'react';
import { godsEyeViewEndpoint, godsEyeViewError, timestamp, type GodsEyeViewApi } from './godsEyeViewAdminState';

type SensorReading = { id: string; sensor_id: string; sensor_type: string; observed_at: string; department: string; municipality: string; locality: string; value: number; unit: string; status: string };
type SensorPage = { items: SensorReading[]; total: number; page: number; version: string };
const statuses: Record<string, string> = { normal: 'Normal', warning: 'Warning', critical: 'Critical' };

export function GodsEyeViewSensorReadings({ api, timeZone, family, families, searchIcon, refreshIcon, active = true }: {
  api: GodsEyeViewApi; timeZone: string; family: string; families: Record<string, string>; searchIcon: ReactNode; refreshIcon: ReactNode; active?: boolean;
}) {
  const [readings, setReadings] = useState<(SensorPage & { requestKey: string }) | null>(null);
  const [search, setSearch] = useState(''), [query, setQuery] = useState(''), [status, setStatus] = useState('');
  const [order, setOrder] = useState<'asc' | 'desc'>('desc');
  const [pagination, setPagination] = useState({ page: 1, filters: '' }), [pageSize, setPageSize] = useState(20);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [refresh, setRefresh] = useState(0);
  const filters = JSON.stringify([family, query, status, order, pageSize]);
  const requestedPage = pagination.filters === filters ? pagination.page : 1;
  const requestKey = JSON.stringify([filters, requestedPage]);
  const result = readings?.requestKey === requestKey ? readings : null;
  const currentPage = result?.page || requestedPage;
  useEffect(() => { if (pagination.filters !== filters) setPagination({ page: 1, filters }); }, [pagination.filters, filters]);
  useEffect(() => {
    if (search.trim() === query) return;
    const timer = window.setTimeout(() => setQuery(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search, query]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController(); let loading = false, page = currentPage;
    async function load(initial: boolean) {
      if (loading || document.hidden) return;
      loading = true; if (initial) setBusy(true);
      try {
        const params = new URLSearchParams({ page: String(page), limit: String(pageSize), order,
          ...(family ? { family } : {}), ...(query ? { q: query } : {}), ...(status ? { status } : {}) });
        const next = await api<SensorPage>(`${godsEyeViewEndpoint}/sensors/readings?${params}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        page = next.page;
        setReadings({ ...next, requestKey }); setError('');
      } catch (reason) { if (!controller.signal.aborted) setError(godsEyeViewError(reason)); }
      finally { loading = false; if (!controller.signal.aborted) setBusy(false); }
    }
    void load(true);
    const timer = window.setInterval(() => void load(false), 5000);
    const resume = () => { void load(true); };
    document.addEventListener('visibilitychange', resume);
    return () => { controller.abort(); window.clearInterval(timer); document.removeEventListener('visibilitychange', resume); };
  }, [api, refresh, active, requestKey]);
  const items = result?.items || [], pages = Math.max(1, Math.ceil((result?.total || 0) / pageSize));
  const first = items.length ? (currentPage - 1) * pageSize + 1 : 0, last = items.length ? first + items.length - 1 : 0;
  const searching = busy || search.trim() !== query;
  return <section className="gods-eye-view-posts gods-eye-view-sensor-readings" aria-label="Sensor readings">
    {error && <p className="gods-eye-view-error" role="alert">{error}</p>}
    <div className="admin-panel"><div className="admin-toolbar">
      <form className="search" role="search" aria-label="Sensor readings" onSubmit={event => { event.preventDefault(); setQuery(search.trim()); }}>
        <label><span className="sr-only">Search sensor readings</span><input type="search" maxLength={200} value={search} onChange={event => setSearch(event.target.value)} placeholder="Search by sensor, type or location" /></label>
        <button className="search-submit" type="submit" aria-label="Search sensor readings" title="Search sensor readings">{searchIcon}</button>
      </form>
      <label className="gods-eye-view-network-filter"><span className="sr-only">Reading status</span><select value={status} onChange={event => setStatus(event.target.value)}>
        <option value="">All statuses</option>{Object.entries(statuses).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      <div className="toolbar-actions"><button className="toolbar-icon" type="button" disabled={busy} onClick={() => setRefresh(value => value + 1)} aria-label="Refresh sensor readings" title="Refresh sensor readings">{refreshIcon}</button></div>
    </div><div className="table-wrap gods-eye-view-post-table" aria-busy={busy || searching}><table aria-label="Latest published sensor readings"><thead><tr>
      <th scope="col">Sensor ID</th><th scope="col">Type</th><th scope="col">Location</th><th scope="col">Reading</th>
      <th scope="col" aria-sort={order === 'desc' ? 'descending' : 'ascending'}><button type="button" className="gods-eye-view-sort" aria-label={`Sort by observation date, ${order === 'desc' ? 'oldest' : 'newest'} first`} onClick={() => setOrder(value => value === 'desc' ? 'asc' : 'desc')}>
        Observed at<span aria-hidden="true">{order === 'desc' ? '↓' : '↑'}</span></button></th><th scope="col">Status</th>
    </tr></thead><tbody>{items.map(reading => <tr key={reading.sensor_id} className={query ? 'gods-eye-view-search-match' : undefined}>
      <td>{reading.sensor_id}</td><td>{families[reading.sensor_type] || reading.sensor_type}</td><td>{[...new Set([reading.locality, reading.municipality, reading.department].filter(Boolean))].join(', ') || 'Unknown'}</td>
      <td>{reading.value} {reading.unit}</td><td className="gods-eye-view-post-created"><time dateTime={reading.observed_at}>{timestamp(reading.observed_at, timeZone)}</time></td>
      <td><span className={`badge gods-eye-view-sensor-status ${reading.status}`}>{statuses[reading.status] || reading.status}</span></td>
    </tr>)}</tbody></table>
      {!result && busy && <LoadingIndicator label="Loading sensor readings…" />}{result && !items.length && <p className="empty" role="status">{family || query || status ? 'No readings match these filters.' : 'No sensor readings published yet.'}</p>}
    </div></div>
    <nav className="gods-eye-view-pagination" aria-label="Sensor reading pages"><label>Rows per page<select value={pageSize} onChange={event => setPageSize(Number(event.target.value))}>{[10, 20, 50, 100].map(size => <option key={size} value={size}>{size}</option>)}</select></label>
      <span aria-live="polite">{first}–{last} of {result?.total || 0}</span><button type="button" className="secondary" disabled={searching || !result || currentPage === 1} onClick={() => { setReadings(null); setPagination({ page: currentPage - 1, filters }); setRefresh(value => value + 1); }}>Previous</button>
      <span>Page {currentPage}</span><button type="button" className="secondary" disabled={searching || !result || currentPage >= pages} onClick={() => { setReadings(null); setPagination({ page: currentPage + 1, filters }); setRefresh(value => value + 1); }}>Next</button></nav>
  </section>;
}
