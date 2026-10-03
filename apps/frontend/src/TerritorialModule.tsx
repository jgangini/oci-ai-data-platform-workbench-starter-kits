import { useEffect, useState } from 'react';

type Api = <T>(path: string, init?: RequestInit) => Promise<T>;
type Module = { status: string; installed: boolean; enabled: boolean; runtime: string; message: string; viewer_url: string };

export function TerritorialModule({ api }: { api: Api }) {
  const [module, setModule] = useState<Module | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function load() {
    try { setModule(await api<Module>('/api/admin/prisma/module')); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Module status unavailable'); }
  }
  useEffect(() => { void load(); }, []);
  useEffect(() => {
    if (module?.status !== 'activating') return;
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [module?.status]);
  async function deploy() {
    setBusy(true); setError('');
    try { setModule(await api<Module>('/api/admin/prisma/module/deploy', { method: 'POST' })); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Module deployment failed'); }
    finally { setBusy(false); }
  }
  return <div className="territorial-module"><p role="status">{module?.message || 'Checking module…'}</p>
    <div className="settings-actions"><button type="button" disabled={busy || !module || module.status === 'activating' || module.enabled} onClick={() => void deploy()}>{module?.enabled ? 'Enabled' : module?.status === 'activating' ? 'Deploying…' : 'Deploy and enable'}</button>
      <a className="settings-link" href="/admin/prisma" aria-label="Territorial Control settings">Settings</a>
      {module?.enabled && <a className="settings-link" href={module.viewer_url}>Open viewer ↗</a>}
    </div>{error && <p role="alert">{error}</p>}
  </div>;
}
