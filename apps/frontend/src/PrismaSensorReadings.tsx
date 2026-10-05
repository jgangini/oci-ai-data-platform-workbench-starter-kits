import { LoadingIndicator } from './LoadingIndicator';
import { useEffect, useState, type ReactNode } from 'react';
import { prismaError, timestamp, type PrismaApi } from './prismaAdminState';

type SensorReading = { id: string; sensor_id: string; sensor_type: string; observed_at: string; department: string; municipality: string; locality: string; value: number; unit: string; status: string };
const normalized = (value: string) => value.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const observed = (reading: SensorReading) => Date.parse(reading.observed_at) || 0;
const statuses: Record<string, string> = { normal: 'Normal', warning: 'Warning', critical: 'Critical' };

export function PrismaSensorReadings({ api, timeZone, family, families, searchIcon, refreshIcon, active = true }: {
  api: PrismaApi; timeZone: string; family: string; families: Record<string, string>; searchIcon: ReactNode; refreshIcon: ReactNode; active?: boolean;
}) {
  const [readings, setReadings] = useState<SensorReading[] | null>(null);
  const [search, setSearch] = useState(''), [query, setQuery] = useState(''), [status, setStatus] = useState('');
  const [order, setOrder] = useState<'asc' | 'desc'>('desc');
  const [pagination, setPagination] = useState({ page: 1, filters: '' }), [pageSize, setPageSize] = useState(20);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [refresh, setRefresh] = useState(0);
  useEffect(() => {
    if (search.trim() === query) return;
    const timer = window.setTimeout(() => setQuery(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search, query]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController(); let loading = false;
    async function load(initial: boolean) {
      if (loading) return;
      loading = true; if (initial) setBusy(true);
      try {
        const snapshot = await api<{ sensors?: SensorReading[] }>('/api/prisma/snapshot', { signal: controller.signal });
        if (controller.signal.aborted) return;
        const latest = new Map<string, SensorReading>();
        for (const reading of snapshot.sensors || []) {
          if (!reading?.sensor_id || !reading.id) continue;
          const previous = latest.get(reading.sensor_id);
          if (!previous || observed(reading) > observed(previous) || (observed(reading) === observed(previous) && reading.id < previous.id)) latest.set(reading.sensor_id, reading);
        }
        setReadings([...latest.values()]); setError('');
      } catch (reason) { if (!controller.signal.aborted) setError(prismaError(reason)); }
      finally { loading = false; if (!controller.signal.aborted) setBusy(false); }
    }
    void load(true);
    const timer = window.setInterval(() => void load(false), 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [api, refresh, active]);
  const match = normalized(query);
  // ponytail: the published snapshot is bounded to 5,000 latest readings; use API pagination if historical readings are added.
  const filtered = (readings || []).filter(reading => (!family || reading.sensor_type === family) && (!status || reading.status === status)
    && normalized([reading.sensor_id, reading.sensor_type, families[reading.sensor_type], reading.department, reading.municipality, reading.locality, reading.status].join(' ')).includes(match))
    .sort((a, b) => (observed(a) - observed(b)) * (order === 'asc' ? 1 : -1) || a.id.localeCompare(b.id));
  const filters = JSON.stringify([family, query, status, order, pageSize]);
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize)), currentPage = Math.min(pagination.filters === filters ? pagination.page : 1, pages);
  useEffect(() => { if (pagination.filters !== filters || pagination.page !== currentPage) setPagination({ page: currentPage, filters }); }, [pagination, filters, currentPage]);
  const items = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const first = items.length ? (currentPage - 1) * pageSize + 1 : 0, last = items.length ? first + items.length - 1 : 0;
  const searching = search.trim() !== query;
  return <section className="prisma-posts prisma-sensor-readings" aria-label="Sensor readings">
    {error && <p className="prisma-error" role="alert">{error}</p>}
    <div className="admin-panel"><div className="admin-toolbar">
      <form className="search" role="search" aria-label="Sensor readings" onSubmit={event => { event.preventDefault(); setQuery(search.trim()); }}>
        <label><span className="sr-only">Search sensor readings</span><input type="search" maxLength={200} value={search} onChange={event => setSearch(event.target.value)} placeholder="Search by sensor, type or location" /></label>
        <button className="search-submit" type="submit" aria-label="Search sensor readings" title="Search sensor readings">{searchIcon}</button>
      </form>
      <label className="prisma-network-filter"><span className="sr-only">Reading status</span><select value={status} onChange={event => setStatus(event.target.value)}>
        <option value="">All statuses</option>{Object.entries(statuses).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      <div className="toolbar-actions"><button className="toolbar-icon" type="button" disabled={busy} onClick={() => setRefresh(value => value + 1)} aria-label="Refresh sensor readings" title="Refresh sensor readings">{refreshIcon}</button></div>
    </div><div className="table-wrap prisma-post-table" aria-busy={busy || searching}><table aria-label="Latest published sensor readings"><thead><tr>
      <th scope="col">Sensor ID</th><th scope="col">Type</th><th scope="col">Location</th><th scope="col">Reading</th>
      <th scope="col" aria-sort={order === 'desc' ? 'descending' : 'ascending'}><button type="button" className="prisma-sort" aria-label={`Sort by observation date, ${order === 'desc' ? 'oldest' : 'newest'} first`} onClick={() => setOrder(value => value === 'desc' ? 'asc' : 'desc')}>
        Observed at<span aria-hidden="true">{order === 'desc' ? '↓' : '↑'}</span></button></th><th scope="col">Status</th>
    </tr></thead><tbody>{items.map(reading => <tr key={reading.sensor_id} className={query ? 'prisma-search-match' : undefined}>
      <td>{reading.sensor_id}</td><td>{families[reading.sensor_type] || reading.sensor_type}</td><td>{[...new Set([reading.locality, reading.municipality, reading.department].filter(Boolean))].join(', ') || 'Unknown'}</td>
      <td>{reading.value} {reading.unit}</td><td className="prisma-post-created"><time dateTime={reading.observed_at}>{timestamp(reading.observed_at, timeZone)}</time></td>
      <td><span className={`badge prisma-sensor-status ${reading.status}`}>{statuses[reading.status] || reading.status}</span></td>
    </tr>)}</tbody></table>
      {!readings && busy && <LoadingIndicator label="Loading sensor readings…" />}{readings && !items.length && <p className="empty" role="status">{family || query || status ? 'No readings match these filters.' : 'No sensor readings published yet.'}</p>}
    </div></div>
    <nav className="prisma-pagination" aria-label="Sensor reading pages"><label>Rows per page<select value={pageSize} onChange={event => setPageSize(Number(event.target.value))}>{[10, 20, 50, 100].map(size => <option key={size} value={size}>{size}</option>)}</select></label>
      <span aria-live="polite">{first}–{last} of {filtered.length}</span><button type="button" className="secondary" disabled={searching || currentPage === 1} onClick={() => setPagination({ page: currentPage - 1, filters })}>Previous</button>
      <span>Page {currentPage}</span><button type="button" className="secondary" disabled={searching || currentPage === pages} onClick={() => setPagination({ page: currentPage + 1, filters })}>Next</button></nav>
  </section>;
}
