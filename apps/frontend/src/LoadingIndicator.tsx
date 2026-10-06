export function LoadingIndicator({ label, inline = false }: { label: string; inline?: boolean }) {
  return <span className={`loading-indicator${inline ? ' loading-indicator-inline' : ''}`} role="status">
    <span className="progress-orbit" aria-hidden="true" />
    <span className="sr-only">{label}</span>
  </span>;
}
