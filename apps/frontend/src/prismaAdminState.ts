export type PrismaApi = <T>(path: string, init?: RequestInit) => Promise<T>;
export type Source = {
  platform: string; enabled: boolean; mode: 'simulation' | 'real'; query: string;
  interval_minutes: number; secret_ref: string; credential_configured: boolean;
  status: string; last_run_at?: string; next_due?: string; last_error?: string;
  last_received_count?: number | null; capture_running?: boolean; capture_state?: string;
  correlation_window_minutes: number; report_thresholds: { low: number; medium: number; high: number };
  config_version: number;
};
export type SourceEditor = { draft: Source; saved: Source; token: string };
export type Attachment = { id: string; type: string; mime_type: string; url: string; alt_text?: string };
export type Post = { id: string; platform: string; username: string; display_name: string; country: string; city: string;
  locality: string; location_method?: string; text: string; published_at: string; ingested_at?: string; processing_status: string;
  mode: string; url?: string; attachments: Attachment[] };
export type PostPage = { items: Post[]; next_cursor: string | null; total: number; version: string };
export type PostListing = { page: PostPage | null; changed: boolean };
export const networkNames: Record<string, string> = { x: 'X', facebook: 'Facebook', instagram: 'Instagram', tiktok: 'TikTok' };
export const prismaEndpoint = '/api/admin/prisma';
export const timestamp = (value?: string | null) => value ? new Date(value).toLocaleString('en-GB', { timeZone: 'America/Bogota' }) : 'Not available';
export function sourceDefaults(source: Source): Source {
  return { ...source, correlation_window_minutes: source.correlation_window_minutes ?? 30,
    report_thresholds: source.report_thresholds ?? { low: 5, medium: 10, high: 20 }, config_version: source.config_version ?? 0 };
}
export function sourceDirty({ draft, saved, token }: SourceEditor): boolean {
  return !!token || ['enabled', 'mode', 'query', 'interval_minutes', 'correlation_window_minutes']
    .some(key => draft[key as keyof Source] !== saved[key as keyof Source])
    || (['low', 'medium', 'high'] as const).some(level => draft.report_thresholds[level] !== saved.report_thresholds[level]);
}
export function refreshedEditor(editor: SourceEditor, source: Source): SourceEditor {
  return sourceDirty(editor) || source.config_version < editor.saved.config_version ? editor : { draft: source, saved: source, token: '' };
}
export function refreshedPosts(previous: PostListing, result: PostPage, replace: boolean): PostListing {
  if (!previous.page || replace) return { page: result, changed: false };
  if (previous.page.items.map(item => item.id).join('|') === result.items.map(item => item.id).join('|')) return { page: result, changed: previous.changed };
  const updates = new Map(result.items.map(item => [item.id, item]));
  return { changed: true, page: { ...previous.page, items: previous.page.items.map(item => updates.get(item.id) || item) } };
}
export function captureState(source: Source): string {
  if (source.last_error || source.capture_state === 'error') return 'Error';
  const states: Record<string, string> = { running: 'Running', scheduled: 'Scheduled', capturing: 'Capturing', paused: 'Paused' };
  if (source.capture_state && states[source.capture_state]) return states[source.capture_state];
  if (!source.capture_running) return 'Paused';
  if (source.status === 'capturing') return 'Capturing';
  return source.next_due ? 'Scheduled' : 'Running';
}
export function safeMediaUrl(value: string): string | null {
  if (/^\/api\/admin\/prisma\/media\/[A-Za-z0-9_-]+\/[A-Za-z0-9_-]+\.(svg|webp|png|jpe?g|mp4|webm)$/.test(value)) return value;
  try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password ? url.href : null; }
  catch { return null; }
}
export function prismaError(error: unknown): string {
  if (error && typeof error === 'object' && 'status' in error && error.status === 401) window.location.assign('/admin/login');
  return error instanceof Error ? error.message : 'The operation could not be completed.';
}
