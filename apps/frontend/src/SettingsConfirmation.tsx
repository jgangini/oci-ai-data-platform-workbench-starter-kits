import { useEffect, useId, useRef } from 'react';

export function SettingsConfirmation({ title, description = 'The following settings will change:', changes = [], confirmLabel = 'Save changes', onCancel, onConfirm }: {
  title: string; description?: string; changes?: string[]; confirmLabel?: string; onCancel: () => void; onConfirm: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null), titleId = useId(), descriptionId = useId();
  useEffect(() => {
    const origin = document.activeElement, node = dialog.current;
    node?.showModal();
    return () => { node?.close(); if (origin instanceof HTMLElement && origin.isConnected) origin.focus(); };
  }, []);
  return <dialog ref={dialog} className="confirm-modal territorial-reset-dialog" aria-labelledby={titleId} aria-describedby={descriptionId} onCancel={event => { event.preventDefault(); onCancel(); }} onClose={onCancel}>
    <div className="confirm-content"><h2 id={titleId}>{title}</h2><p id={descriptionId}>{description}</p>{changes.length > 0 && <ul>{changes.map(change => <li key={change}>{change}</li>)}</ul>}</div>
    <footer><button type="button" onClick={onCancel} autoFocus>Cancel</button><button type="button" className="confirm-primary" onClick={onConfirm}>{confirmLabel}</button></footer>
  </dialog>;
}
