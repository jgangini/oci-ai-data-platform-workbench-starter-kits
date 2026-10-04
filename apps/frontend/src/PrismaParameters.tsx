import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from 'react';
import { ParameterConfirmation, PrismaOciParameters } from './PrismaOciParameters';
import { prismaEndpoint, prismaError, type PrismaApi } from './prismaAdminState';

type Provider = { id: string; label: string; configured: boolean; fields: { id: string; label: string; configured: boolean; secret: boolean; client_exposed?: boolean }[] };
type Parameters = { revision: string; providers: Provider[]; message?: string; runtime_status?: 'applied' | 'pending' };
const providerHelp: Record<string, { url: string; description: string }> = {
  'google-maps': { url: 'https://developers.google.com/maps/documentation/tile/get-api-key', description: 'Provides photorealistic 3D map tiles and place search.' },
  openai: { url: 'https://platform.openai.com/api-keys', description: 'Provides real-time voice control and summaries of the current map view.' },
  aisstream: { url: 'https://aisstream.io/account', description: 'Streams vessel positions for the Live Vessels layer.' },
  firms: { url: 'https://firms.modaps.eosdis.nasa.gov/api/map_key/', description: 'Provides satellite detections for the Active Fires layer.' },
  tomtom: { url: 'https://docs.tomtom.com/platform/documentation/my-tomtom/how-to-get-a-tomtom-api-key', description: 'Provides live road traffic flow for the Traffic layer.' },
  'cesium-ion': { url: 'https://ion.cesium.com/tokens', description: 'Provides Bing imagery and world terrain for the globe.' },
  opensky: { url: 'https://openskynetwork.github.io/opensky-api/rest.html#authentication', description: 'Provides aircraft positions for the Flights layer with authenticated polling.' },
  'launch-library': { url: 'https://lldev.thespacedevs.com/docs', description: 'Provides recent rocket launches and mission details for Space Missions.' },
};

export function PrismaProviderParameters({ api, provider, revision, active, onSaved, onRefresh }: { api: PrismaApi; provider: Provider; revision: string; active: boolean; onSaved: (next: Parameters) => void; onRefresh?: () => void }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [confirm, setConfirm] = useState(false);
  const request = useRef<AbortController | null>(null);
  const replacement = Object.fromEntries(Object.entries(values).map(([key, value]) => [key, value.trim()]).filter(([, value]) => value));
  const dirty = Object.keys(replacement).length > 0;
  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => { if (!active) { request.current?.abort(); request.current = null; setBusy(''); setConfirm(false); } }, [active]);
  async function action(kind: 'save' | 'test') {
    if (request.current) return;
    const controller = new AbortController(); request.current = controller;
    setBusy(kind); setConfirm(false); setError(''); setMessage('');
    try {
      const result = await api<Parameters & { ok?: boolean; status?: string }>(`${prismaEndpoint}/parameters/${encodeURIComponent(provider.id)}${kind === 'test' ? '/test' : ''}`, {
        method: kind === 'save' ? 'PUT' : 'POST', signal: controller.signal, body: JSON.stringify({ expected_revision: revision, values: replacement }),
      });
      if (controller.signal.aborted) return;
      if (kind === 'save') { onSaved(result); setValues({}); setMessage(provider.fields.some(field => field.client_exposed && field.id in replacement) ? 'Saved. Reload the globe page to use the updated browser credentials.' : 'Saved.'); }
      else if (result.ok) setMessage(result.message || 'Test passed.');
      else setError(result.message || 'The provider test did not pass.');
    } catch (reason) { if (!controller.signal.aborted) { setError(prismaError(reason)); onRefresh?.(); } }
    finally { if (!controller.signal.aborted) setBusy(''); if (request.current === controller) request.current = null; }
  }
  return <form className="prisma-source" aria-label={provider.label} onSubmit={(event: FormEvent) => { event.preventDefault(); if (dirty && !busy) setConfirm(true); }}>
    <div className="prisma-source-heading"><h3>{provider.label}</h3><div className="prisma-source-badges">
      <span className={`prisma-mode ${provider.configured ? 'real' : ''}`}>{provider.configured ? 'Configured' : 'Not configured'}</span></div></div>
    <p className="prisma-provider-description">{providerHelp[provider.id]?.description} Blank fields keep their current values. Test checks entered values without saving them.</p>
    <fieldset disabled={!!busy}><div className="prisma-fields">{provider.fields.map(field => <label key={field.id}>{field.label}<input type={field.secret ? 'password' : 'text'} autoComplete="new-password" spellCheck={false} maxLength={512}
      value={values[field.id] || ''} placeholder={field.configured ? 'Configured · leave blank to keep current' : 'Not configured'} onChange={event => { setValues(previous => ({ ...previous, [field.id]: event.target.value })); setError(''); setMessage(''); }} /></label>)}</div>
      {providerHelp[provider.id] && <div className="prisma-parameter-fields-help"><a className="prisma-key-help" href={providerHelp[provider.id].url} target="_blank" rel="noopener noreferrer" aria-label={`Get key information for ${provider.label}`}>GET KEY ↗</a></div>}
      <div className="prisma-source-actions"><button type="submit" disabled={!dirty}>{busy === 'save' ? 'Saving…' : 'Save'}</button><button type="button" className="secondary" disabled={!dirty && !provider.configured} onClick={() => void action('test')}>{busy === 'test' ? 'Testing…' : 'Test'}</button>
        {dirty && <button type="button" className="secondary" onClick={() => setValues({})}>Discard changes</button>}</div>
    </fieldset>
    {message && <p role="status" className="prisma-success">{message}</p>}{error && <p role="alert" className="prisma-error">{error}</p>}
    {confirm && <ParameterConfirmation title={provider.label} changes={provider.fields.filter(field => field.id in replacement).map(field => `${field.label}: replace value`)} onCancel={() => setConfirm(false)} onConfirm={() => void action('save')} />}
  </form>;
}

export function PrismaParameters({ api, active }: { api: PrismaApi; active: boolean }) {
  const [configuration, setConfiguration] = useState<Parameters | null>(null);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState('google-maps');
  const [collapsed, setCollapsed] = useState(false);
  const [ociText, setOciText] = useState<boolean | undefined>();
  const [ociVoice, setOciVoice] = useState<boolean | undefined>();
  const [scrollEdges, setScrollEdges] = useState([false, false]);
  const tabList = useRef<HTMLDivElement | null>(null);
  const tabs = useRef<Record<string, HTMLButtonElement | null>>({});
  const savedRevision = useRef(0);
  const providers = [...(configuration?.providers || []), { id: 'oci-text', label: 'OCI · Text', configured: ociText }, { id: 'oci-voice', label: 'OCI · Voice', configured: ociVoice }];
  const selectedId = providers.some(provider => provider.id === selected) ? selected : providers[0].id;
  function updateScrollEdges() {
    const list = tabList.current;
    if (!list) return;
    const left = list.scrollLeft > 1, right = list.scrollWidth - list.clientWidth - list.scrollLeft > 1;
    setScrollEdges(previous => previous[0] === left && previous[1] === right ? previous : [left, right]);
  }
  useEffect(() => {
    const list = tabList.current;
    if (!active || !list) return;
    updateScrollEdges();
    const observer = new ResizeObserver(updateScrollEdges);
    observer.observe(list);
    return () => observer.disconnect();
  }, [active, providers.length]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController(), generation = savedRevision.current;
    let timer: ReturnType<typeof setTimeout>;
    let pending = configuration?.runtime_status === 'pending';
    const load = async () => {
      setLoading(true);
      try {
        const result = await api<Parameters>(`${prismaEndpoint}/parameters`, { signal: controller.signal });
        if (!controller.signal.aborted && generation === savedRevision.current) {
          setConfiguration(result); setError('');
          pending = result.runtime_status === 'pending';
        }
      } catch (reason) { if (!controller.signal.aborted && generation === savedRevision.current) setError(prismaError(reason)); }
      finally {
        if (!controller.signal.aborted && generation === savedRevision.current) {
          setLoading(false);
          if (pending) timer = setTimeout(() => void load(), 2500);
        }
      }
    };
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [active, refresh]);
  function tabKey(event: KeyboardEvent<HTMLButtonElement>) {
    const offset = ['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : ['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 0;
    if (!offset && !['Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? providers.length - 1 : (providers.findIndex(provider => provider.id === selectedId) + offset + providers.length) % providers.length;
    setSelected(providers[index].id); setCollapsed(false); tabs.current[providers[index].id]?.focus();
  }
  return <section className="prisma-module-content" aria-label="Parameters">
    <div className="prisma-sources-title"><h2>Power up the globe</h2></div>
    <div className="prisma-parameter-navigation">
    <div ref={tabList} id="prisma-provider-tabs" className="settings-tabs prisma-network-tabs prisma-parameter-tabs" role="tablist" aria-label="Providers" onScroll={updateScrollEdges} data-scroll-left={scrollEdges[0]} data-scroll-right={scrollEdges[1]}>{providers.map(provider => {
      const state = provider.configured === undefined ? 'Loading settings' : provider.configured ? 'Configured' : 'Not configured';
      return <button key={provider.id} ref={element => { tabs.current[provider.id] = element; }} id={`prisma-parameter-tab-${provider.id}`} className="settings-tab" type="button" role="tab"
        aria-selected={selectedId === provider.id} aria-expanded={selectedId === provider.id && !collapsed} aria-controls={`prisma-parameter-panel-${provider.id}`} tabIndex={selectedId === provider.id ? 0 : -1} onClick={() => { setCollapsed(value => selectedId === provider.id ? !value : false); setSelected(provider.id); }} onKeyDown={tabKey}>
        {provider.label}<span className={`prisma-capture-dot ${provider.configured === undefined ? 'unavailable' : provider.configured ? '' : 'unconfigured'}`} role="img" aria-label={state} title={state} /></button>;
    })}</div>
    {[-1, 1].map((direction, index) => <button key={direction} type="button" className="secondary prisma-toolbar-button prisma-parameter-scroll" aria-label={`Scroll providers ${direction < 0 ? 'left' : 'right'}`} aria-controls="prisma-provider-tabs" disabled={!scrollEdges[index]} onClick={() => { const list = tabList.current; if (list) list.scrollBy({ left: direction * list.clientWidth * .8 }); }}>
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d={direction < 0 ? 'm14 6-6 6 6 6' : 'm10 6 6 6-6 6'} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
    </button>)}
    </div>
    {configuration?.runtime_status === 'pending' && <p role="status" className="prisma-reset-status">Provider settings are saved. Waiting for the globe service to apply them…</p>}
    {loading && !configuration && <p role="status">Loading parameters…</p>}{error && <p role="alert" className="prisma-error">{error}</p>}
    {error && <button type="button" className="secondary" disabled={loading} onClick={() => setRefresh(value => value + 1)}>Retry settings</button>}
    {configuration?.providers.map(provider => <section key={provider.id} id={`prisma-parameter-panel-${provider.id}`} className="settings-panel" role="tabpanel" aria-labelledby={`prisma-parameter-tab-${provider.id}`} hidden={collapsed || selectedId !== provider.id}>
      <PrismaProviderParameters api={api} provider={provider} revision={configuration.revision} active={active} onRefresh={() => setRefresh(value => value + 1)} onSaved={next => { savedRevision.current++; setConfiguration(next); setError(''); setRefresh(value => value + 1); }} />
    </section>)}
    <section id="prisma-parameter-panel-oci-text" className="settings-panel" role="tabpanel" aria-labelledby="prisma-parameter-tab-oci-text" hidden={collapsed || selectedId !== 'oci-text'}><PrismaOciParameters api={api} active={active} onConfigured={setOciText} /></section>
    <section id="prisma-parameter-panel-oci-voice" className="settings-panel" role="tabpanel" aria-labelledby="prisma-parameter-tab-oci-voice" hidden={collapsed || selectedId !== 'oci-voice'}><PrismaOciParameters api={api} active={active} onConfigured={setOciVoice} voice /></section>
  </section>;
}
