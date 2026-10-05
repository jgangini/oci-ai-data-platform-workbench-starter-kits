import { LoadingIndicator } from './LoadingIndicator';
import { FormEvent, useEffect, useState } from 'react';

type Api = <T>(path: string, init?: RequestInit) => Promise<T>;
type Workspace = { message: string; viewer_url: string; project_access: { workspace_path: string; role: string }; user: { name: string; email: string; material?: { labs?: { lab_id: string; workspace_path: string }[] } } };

export function LocalTerritorialAccess({ api, workspace = false }: { api: Api; workspace?: boolean }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [data, setData] = useState<Workspace | null>(null);
  useEffect(() => {
    if (workspace) void api<Workspace>('/api/local/territorial/workspace').then(setData).catch(() => window.location.assign('/local/gods-eye-view/login'));
  }, [workspace]);
  async function submit(event: FormEvent) {
    event.preventDefault(); setError('');
    try {
      await api('/api/local/territorial/login', { method: 'POST', body: JSON.stringify({ username, password }) });
      window.location.assign('/local/gods-eye-view/workspace');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Sign in failed'); }
    finally { setPassword(''); }
  }
  return <section className="card"><p className="eyebrow">SIMULATED · Local participant access</p><h1>Territorial Control</h1>
    {workspace ? data ? <><p>{data.user.name} · {data.user.email}</p><p>{data.message}</p>
      <h2>AIDP workspace preview</h2><p>Territorial Control · {data.project_access.role}: <code>{data.project_access.workspace_path}</code></p>
      <h3>Additional starter kits</h3><ul>{data.user.material?.labs?.map(lab => <li key={lab.lab_id}>{lab.lab_id}: <code>{lab.workspace_path}</code></li>)}</ul>
      <p>This preview represents the local provisioning result. It does not connect to a cloud AIDP workspace.</p>
      <a href={data.viewer_url}>Open God’s Eye View →</a></> : <LoadingIndicator label="Loading workspace…" />
      : <form onSubmit={submit}><p>Use the credentials in your local welcome file. No email is sent.</p>
        <label>Email<input value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" required /></label>
        <label>Password<input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" required /></label>
        <button type="submit">Sign in</button></form>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
