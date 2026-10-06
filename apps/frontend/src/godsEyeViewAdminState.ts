export type GodsEyeViewApi = <T>(path: string, init?: RequestInit) => Promise<T>;
export type Source = {
  platform: string; enabled: boolean; mode: 'Synthetic' | 'simulation' | 'real'; query: string;
  interval_minutes: number; synthetic_batch_max: number; secret_ref: string; credential_configured: boolean;
  status: string; last_run_at?: string; next_due?: string; last_error?: string;
  last_received_count?: number | null; capture_running?: boolean; capture_state?: string;
  correlation_window_minutes: number; report_thresholds: { low: number; medium: number; high: number };
  config_version: number;
};
export type SourceEditor = { draft: Source; saved: Source; token: string };
export type CaptureSchedule = { start_at: string | null; interval_minutes: number; config_version: number };
export type Attachment = { id: string; type: string; mime_type: string; url: string; alt_text?: string; origin?: string };
export type Post = { id: string; platform: string; username: string; display_name: string; country: string; city: string;
  locality: string; location_method?: string; text: string; published_at: string | null; captured_at?: string; ingested_at?: string; processing_status: string;
  mode: string; url?: string; attachments: Attachment[] };
export type PostPage = { items: Post[]; next_cursor: string | null; total: number; version: string };
export type PostListing = { page: PostPage | null; changed: boolean };
export const networkNames: Record<string, string> = { x: 'X', facebook: 'Facebook', instagram: 'Instagram', tiktok: 'TikTok' };
export const godsEyeViewEndpoint = '/api/admin/gods-eye-view';
export const sourceQueryLimit = 1000;
export const timestamp = (value?: string | null, timeZone = 'America/Bogota') => value ? new Date(value).toLocaleString('en-GB', { timeZone }) : 'Not available';
export const postStatus = (value: string) => value === 'processed' ? 'Processed' : 'New';
export function sourceDefaults(source: Source): Source {
  return { ...source, mode: source.mode === 'simulation' ? 'Synthetic' : source.mode, synthetic_batch_max: source.synthetic_batch_max ?? 3, correlation_window_minutes: source.correlation_window_minutes ?? 30,
    report_thresholds: source.report_thresholds ?? { low: 5, medium: 10, high: 20 }, config_version: source.config_version ?? 0 };
}
export function sourceDirty({ draft, saved, token }: SourceEditor): boolean {
  return (draft.mode === 'real' && !!token) || (draft.mode !== 'real' && draft.synthetic_batch_max !== saved.synthetic_batch_max) || ['mode', 'query', 'correlation_window_minutes']
    .some(key => draft[key as keyof Source] !== saved[key as keyof Source])
    || (['low', 'medium', 'high'] as const).some(level => draft.report_thresholds[level] !== saved.report_thresholds[level]);
}
export function sourcePayload({ draft, saved, token }: SourceEditor) {
  const { mode, query, synthetic_batch_max, correlation_window_minutes, report_thresholds } = draft;
  if (query.length > sourceQueryLimit) throw new Error(`Searches must contain at most ${sourceQueryLimit} characters in total.`);
  if (!(report_thresholds.low > 0 && report_thresholds.low < report_thresholds.medium && report_thresholds.medium < report_thresholds.high)) {
    throw new Error('Report activity thresholds must be positive and increase from Low to Medium to High.');
  }
  if (mode !== 'real' && (!Number.isInteger(synthetic_batch_max) || synthetic_batch_max < 1 || synthetic_batch_max > 100)) {
    throw new Error('Synthetic records per capture must be a whole number from 1 to 100.');
  }
  return { mode: mode === 'simulation' ? 'Synthetic' : mode, query, correlation_window_minutes, report_thresholds,
    ...(mode !== 'real' ? { synthetic_batch_max } : {}),
    expected_revision: saved.config_version, ...(mode === 'real' && token ? { bearer_token: token } : {}) };
}
export function scheduleLocalTime(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
}
export function schedulePayload(start: string, interval: number, revision: number) {
  const date = new Date(start);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(start) || !Number.isFinite(date.getTime())
    || scheduleLocalTime(date.toISOString()) !== (start.length === 16 ? start + ':00' : start)) {
    throw new Error('Choose a valid scheduled date and time in your browser time zone.');
  }
  if (!Number.isInteger(interval) || interval < 1 || interval > 1440) throw new Error('Capture interval must be a whole number from 1 to 1440 minutes.');
  return { start_at: date.toISOString(), interval_minutes: interval, expected_revision: revision };
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
  return source.enabled && source.capture_running ? 'Running' : 'Paused';
}
export function safeMediaUrl(value: string): string | null {
  // Compatibility: saved publications may retain either authenticated legacy media path.
  if (/^\/api\/admin\/(?:gods-eye-view|territorial|prisma)\/media\/[A-Za-z0-9_-]+\/[A-Za-z0-9_-]+\.(svg|webp|png|jpe?g|mp4|webm)$/.test(value)) return value;
  try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password ? url.href : null; }
  catch { return null; }
}
export function godsEyeViewError(error: unknown): string {
  if (error && typeof error === 'object' && 'status' in error && error.status === 401) window.location.assign('/admin/login');
  return error instanceof Error ? error.message : 'The operation could not be completed.';
}
