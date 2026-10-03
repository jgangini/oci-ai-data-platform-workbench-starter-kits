import { FormEvent, useEffect, useState } from 'react';
import './prisma.css';

type Api = <T>(path: string, init?: RequestInit) => Promise<T>;
type Source = {
  platform: string; enabled: boolean; mode: 'simulation' | 'real'; query: string;
  interval_minutes: number; secret_ref: string; credential_configured: boolean;
  status: string; last_run_at?: string; next_due?: string; last_error?: string;
  last_received_count?: number | null;
};
type Simulation = { status: string; elapsed_seconds: number; duration_seconds: number };
type Configuration = { sources: Source[]; simulation: Simulation; runtime: string };
const endpoint = '/api/admin/prisma';
const labels: Record<string, string> = { x: 'X', facebook: 'Facebook', instagram: 'Instagram', tiktok: 'TikTok' };
const timestamp = (value?: string) => value ? new Date(value).toLocaleString('es-CO', { timeZone: 'America/Bogota' }) : 'Sin ejecuciones';
function errorMessage(error: unknown) {
  if (error && typeof error === 'object' && 'status' in error && error.status === 401) window.location.assign('/admin/login');
  return error instanceof Error ? error.message : 'No fue posible completar la operación.';
}

function SourceCard({ source, api }: { source: Source; api: Api }) {
  const [draft, setDraft] = useState(source);
  const [saved, setSaved] = useState(source);
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const overdueSeconds = draft.enabled && draft.mode === 'real' && draft.next_due ? Math.max(0, Math.floor((Date.now() - Date.parse(draft.next_due)) / 1000)) : 0;
  const [error, setError] = useState('');
  const dirty = ['enabled', 'mode', 'query', 'interval_minutes'].some((key) => draft[key as keyof Source] !== saved[key as keyof Source]) || !!token;
  useEffect(() => { setDraft(source); setSaved(source); }, [source]);
  async function action(kind: 'save' | 'test' | 'run') {
    setBusy(true); setMessage(''); setError('');
    try {
      if (kind === 'save') {
        const { enabled, mode, query, interval_minutes } = draft;
        const updated = await api<Source>(`${endpoint}/sources/${encodeURIComponent(source.platform)}`, {
          method: 'PUT', body: JSON.stringify({ enabled, mode, query, interval_minutes, ...(token ? { bearer_token: token } : {}) }),
        });
        setDraft(updated);
        setSaved(updated);
        setToken(''); setMessage('Configuración guardada.');
      } else {
        const result = await api<{ status: string; message: string; source: Source }>(`${endpoint}/sources/${encodeURIComponent(source.platform)}/${kind}`, { method: 'POST' });
        setDraft(result.source); setSaved(result.source); setMessage(result.message);
      }
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }
  return <form className="prisma-source" onSubmit={(event: FormEvent) => { event.preventDefault(); void action('save'); }}>
    <div className="prisma-source-heading"><h2>{labels[source.platform] || source.platform}</h2><span className={`prisma-mode ${draft.mode}`}>{draft.mode === 'real' ? 'REAL' : 'SIMULADO'}</span></div>
    <fieldset disabled={busy}>
      <label className="prisma-toggle"><input type="checkbox" checked={draft.enabled} onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} /> Fuente habilitada</label>
      <div className="prisma-fields"><label>Modo<select value={draft.mode} onChange={(event) => setDraft({ ...draft, mode: event.target.value as Source['mode'] })}><option value="simulation">SIMULADO</option><option value="real" disabled={source.platform !== 'x'}>REAL{source.platform !== 'x' ? ' · pendiente' : ''}</option></select></label>
        <label>Intervalo (minutos)<input type="number" min="1" max="1440" required value={draft.interval_minutes} onChange={(event) => setDraft({ ...draft, interval_minutes: Number(event.target.value) })} /></label></div>
      <label>Consulta o criterios permitidos<textarea rows={2} maxLength={512} value={draft.query} onChange={(event) => setDraft({ ...draft, query: event.target.value })} placeholder="Ej. Bogotá inundación" /></label>
      <label>Credencial asociada<input value={draft.secret_ref || ''} readOnly title="Referencia administrada por el kit" /></label>
      {source.platform !== 'x' && <small>Disponible en simulación. La lectura real requiere habilitar el conector de esta plataforma.</small>}
      <label>Actualizar token<input type="password" autoComplete="new-password" value={token} onChange={(event) => setToken(event.target.value)} placeholder={draft.credential_configured ? 'Configurado · vacío conserva el actual' : 'Sin credencial configurada'} /></label>
      <small>El token solo se envía al guardar; nunca se vuelve a mostrar.</small>
      <div className="prisma-source-actions"><button type="submit">Guardar</button><button className="secondary" type="button" disabled={dirty} onClick={() => void action('test')}>Probar</button><button className="secondary" type="button" disabled={dirty || !draft.enabled} onClick={() => void action('run')}>Ejecutar ahora</button></div>
      {dirty && <small>Guarda los cambios antes de probar o ejecutar la fuente.</small>}
    </fieldset>
    <dl className="prisma-source-status"><div><dt>Estado</dt><dd>{draft.status}</dd></div><div><dt>Última ejecución</dt><dd>{timestamp(draft.last_run_at)}</dd></div><div><dt>Próxima ejecución</dt><dd>{draft.next_due ? timestamp(draft.next_due) : 'Sin programación'}</dd></div></dl>
    <dl className="prisma-source-status"><div><dt>Registros recibidos · última captura exitosa</dt><dd>{draft.mode === 'simulation' ? 'No aplica · simulación' : draft.last_received_count ?? 'Sin captura exitosa'}</dd></div><div><dt>Retraso de captura</dt><dd>{draft.mode === 'real' ? `${overdueSeconds} s` : 'No aplica · simulación'}</dd></div></dl>
    {draft.last_error && <p className="prisma-error">{draft.last_error}</p>}
    {message && <p role="status" className="prisma-success">{message}</p>}
    {error && <p role="alert" className="prisma-error">{error}</p>}
  </form>;
}

export function PrismaAdmin({ api }: { api: Api }) {
  const [config, setConfig] = useState<Configuration | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function load(signal?: AbortSignal) {
    setError('');
    try { setConfig(await api<Configuration>(`${endpoint}/sources`, { signal })); }
    catch (reason) { if (!signal?.aborted) setError(errorMessage(reason)); }
  }
  useEffect(() => { const controller = new AbortController(); void load(controller.signal); return () => controller.abort(); }, []);
  async function simulation(action: 'start' | 'pause' | 'resume' | 'reset') {
    setBusy(true); setError('');
    try { await api(`${endpoint}/simulation`, { method: 'POST', body: JSON.stringify({ action }) }); await load(); }
    catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }
  return <section className="prisma-admin">
    <div className="prisma-heading"><div><p className="eyebrow">PRISMA · BOGOTÁ</p><h1>Fuentes y simulación</h1><p>Configura canales específicos y contrasta su evidencia en el tablero.</p></div><a className="prisma-open" href="/prisma/">Abrir visor GodEye ↗</a></div>
    {error && <p role="alert" className="prisma-error">{error}</p>}
    {!config && !error && <p role="status">Cargando configuración…</p>}
    {config && <>
      <section className="prisma-simulation" aria-label="Control del escenario simulado">
        <div><span className="prisma-mode simulation">SIMULADO</span><h2>Escenario de inundación</h2><p>Reproduce señales sociales, reportes institucionales y sensores para practicar la validación.</p><p className="prisma-runtime">Estado: <strong>{config.simulation.status}</strong> · {Math.floor(config.simulation.elapsed_seconds)} / {config.simulation.duration_seconds} s · {config.runtime === 'aidp' ? 'AIDP' : 'Demostración local'}</p></div>
        <div className="prisma-simulation-actions"><button type="button" disabled={busy || config.simulation.status === 'running'} onClick={() => void simulation(config.simulation.status === 'paused' ? 'resume' : 'start')}>{config.simulation.status === 'paused' ? 'Reanudar' : 'Iniciar escenario'}</button><button className="secondary" type="button" disabled={busy || config.simulation.status !== 'running'} onClick={() => void simulation('pause')}>Pausar</button><button className="secondary" type="button" disabled={busy} onClick={() => void simulation('reset')}>Reiniciar simulación</button></div>
      </section>
      <div className="prisma-sources-title"><h2>Redes seleccionadas</h2><button className="secondary" type="button" disabled={busy} onClick={() => void load()}>Actualizar estados</button></div>
      <div className="prisma-source-grid">{config.sources.map((source) => <SourceCard key={source.platform} source={source} api={api} />)}</div>
    </>}
  </section>;
}
