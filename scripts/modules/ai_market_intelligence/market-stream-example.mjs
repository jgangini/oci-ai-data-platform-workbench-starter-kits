// Consume normalized public packets from the backend MarketGatewayStream, never raw model/tool frames.
export class ReplayRequired extends Error {
  constructor(afterSequence) {
    super('Recover this request with replay before applying more events.');
    this.afterSequence = afterSequence;
  }
}

export function createMarketView(requestId) {
  if (typeof requestId !== 'string' || !requestId || requestId.length > 128) throw new TypeError('Invalid request identity');
  return { requestId, cursor: 0, blocks: new Map(), artifacts: new Map(), references: new Map(),
    events: new Map(), response: null, final: null };
}

function canonical(value) {
  return JSON.stringify(value, (_key, item) => item && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b))) : item);
}

function unsafeMediaHost(host) {
  return !host.includes('.') || /(?:^|\.)(?:localhost|local|localdomain|internal|onion|test|invalid)$/u.test(host)
    || host === 'home.arpa' || host.endsWith('.home.arpa') || /^[\d.]+$/u.test(host) || host.includes(':');
}

function validateMediaURL(value) {
  if (typeof value !== 'string' || value.length > 4096 || /[\s\\\u0000-\u001f\u007f]/u.test(value))
    throw new TypeError('Invalid media URL');
  const media = new URL(value);
  if (media.protocol !== 'https:' || media.username || media.password || media.port || media.hash
      || unsafeMediaHost(media.hostname.toLowerCase())) throw new TypeError('Unsafe media reference');
}

function validateMediaAsset(asset) {
  validateMediaURL(asset.url);
  if (!['image', 'video'].includes(asset.kind) || !['direct', 'page'].includes(asset.delivery)
      || !['attachment', 'thumbnail', 'preview'].includes(asset.role)) throw new TypeError('Unsafe media reference');
}

function validateMediaManifest(assets) {
  if (!Array.isArray(assets) || assets.length > 8) throw new TypeError('Invalid media manifest');
  for (const asset of assets) validateMediaAsset(asset);
}

function manifests(view, references = [], artifacts = []) {
  if (!Array.isArray(references) || !Array.isArray(artifacts) || references.length > 800 || artifacts.length > 800)
    throw new TypeError('Invalid public manifest');
  for (const reference of references) {
    const url = new URL(reference.url);
    if (url.protocol !== 'https:' || url.username || url.password || url.port) throw new TypeError('Unsafe source link');
    if (reference.media !== undefined) {
      validateMediaManifest(reference.media);
    }
    const prior = view.references.get(reference.evidence_id);
    if (prior && prior.url !== reference.url) throw new TypeError('Evidence identity changed destination');
    view.references.set(reference.evidence_id, { ...prior, ...reference });
  }
  for (const artifact of artifacts) view.artifacts.set(artifact.artifact_id, artifact);
}

function event(view, item) {
  if (item.turn_id !== view.requestId || !Number.isSafeInteger(item.sequence) || item.sequence < 1)
    throw new TypeError('Event belongs to another request or has an invalid sequence');
  const identity = canonical(item);
  const prior = view.events.get(item.event_id);
  if (prior !== undefined) {
    if (prior !== identity) throw new TypeError('An immutable event changed');
    return false;
  }
  if (item.sequence !== view.cursor + 1) throw new ReplayRequired(view.cursor);
  view.events.set(item.event_id, identity);
  view.cursor = item.sequence;
  if (item.block_id) view.blocks.set(item.block_id, item);
  return true;
}

function finalResponse(view, response) {
  if (!response) return;
  view.response = response;
  if (response.status === 'pending' || response.resumable) return;
  view.final = response;
  view.artifacts = new Map();
  manifests(view, response.references, response.artifacts);
}

function snapshot(view, packet) {
  const replay = packet.replay;
  if (!replay || !Number.isSafeInteger(replay.cursor) || replay.cursor < view.cursor)
    throw new TypeError('Invalid replay cursor');
  if (replay.snapshot) {
    view.blocks = new Map();
    view.artifacts = new Map();
    for (const block of Object.values(replay.snapshot.blocks)) {
      if (block.turn_id !== view.requestId) throw new TypeError('Foreign replay block');
      const prior = view.events.get(block.event_id);
      if (prior !== undefined && prior !== canonical(block)) throw new TypeError('An immutable event changed');
      view.events.set(block.event_id, canonical(block));
      view.blocks.set(block.block_id, block);
    }
    manifests(view, [], Object.values(replay.snapshot.artifacts));
    view.cursor = replay.cursor;
  } else {
    for (const item of replay.events) event(view, item);
    if (view.cursor !== replay.cursor) throw new ReplayRequired(view.cursor);
  }
  finalResponse(view, packet.response);
}

function hideWithdrawnArtifacts(view) {
  const retired = new Set([...view.blocks.values()]
    .filter(block => block.type === 'draft.corrected' && block.status === 'failed')
    .flatMap(block => block.evidence_ids));
  for (const [identity, artifact] of view.artifacts) {
    if (artifact.evidence_ids.some(id => retired.has(id))) view.artifacts.delete(identity);
  }
}

export function reduceMarketPacket(current, incoming) {
  const packet = structuredClone(incoming);
  if (packet?.protocol !== 'ai-market-intelligence' || packet.version !== '2.0')
    throw new TypeError('Expected a normalized public current packet');
  const turn = packet.kind === 'event' ? packet.event?.turn_id : packet.request_id;
  if (turn !== current.requestId) throw new TypeError('Packet belongs to another request');
  const view = { ...current, blocks: new Map(current.blocks), artifacts: new Map(current.artifacts),
    references: new Map(current.references), events: new Map(current.events) };
  const handlers = {
    heartbeat: () => {},
    event: () => { if (event(view, packet.event)) manifests(view, packet.references, packet.artifacts); },
    response: () => { manifests(view, packet.references, packet.artifacts); finalResponse(view, packet.response); },
    snapshot: () => { manifests(view, packet.references, packet.artifacts); snapshot(view, packet); },
  };
  if (!Object.hasOwn(handlers, packet.kind)) throw new TypeError('Unknown public packet kind');
  handlers[packet.kind]();
  if (view.final) view.artifacts = new Map(view.final.artifacts.map(artifact => [artifact.artifact_id, artifact]));
  hideWithdrawnArtifacts(view);
  return view;
}
