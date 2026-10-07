import { type KeyboardEvent, useEffect, useId, useRef, useState } from 'react';

export function SearchableCombobox({ label, value, options, onChange, disabled = false, placeholder, className = '' }: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
  disabled?: boolean;
  placeholder?: string;
  className?: string;
}) {
  const id = useId();
  const [search, setSearch] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const activeRef = useRef<HTMLButtonElement>(null);
  const expanded = open && !disabled;
  const query = search.trim().toLowerCase().replaceAll('_', ' ');
  const filtered = options.filter(option => option.label.toLowerCase().replaceAll('_', ' ').includes(query));
  const activeOption = expanded ? filtered[active] : undefined;

  function close() { setOpen(false); setSearch(''); setActive(-1); }
  function select(next: string) {
    if (disabled) return;
    onChange(next); close();
  }
  useEffect(close, [disabled]);
  useEffect(() => {
    if (expanded) activeRef.current?.scrollIntoView({ block: 'nearest' });
  }, [expanded, active, search]);

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (disabled) return;
    switch (event.key) {
      case 'Escape':
        if (expanded) { event.preventDefault(); event.stopPropagation(); }
        close(); break;
      case 'Tab': close(); break;
      case 'ArrowDown':
      case 'ArrowUp':
        event.preventDefault(); setOpen(true);
        setActive(event.key === 'ArrowDown' ? Math.min(active + 1, filtered.length - 1)
          : active < 0 ? filtered.length - 1 : Math.max(active - 1, 0));
        break;
      case 'Enter':
        if (expanded) {
          event.preventDefault();
          if (filtered[active]) select(filtered[active].value);
        }
    }
  }

  return <div className={`lab-combobox searchable-combobox ${className}`.trim()} onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) close();
  }}>
    <label className="lab-combobox-label" htmlFor={id}>{label}</label>
    <input id={id} role="combobox" aria-autocomplete="list" aria-haspopup="listbox"
      aria-expanded={expanded} aria-controls={`${id}-options`}
      aria-activedescendant={activeOption ? `${id}-${active}` : undefined}
      autoComplete="off" placeholder={placeholder} disabled={disabled}
      value={expanded ? search : options.find(option => option.value === value)?.label || value}
      onFocus={() => setOpen(true)} onClick={() => setOpen(true)}
      onChange={event => { setSearch(event.target.value); setActive(0); setOpen(true); }}
      onKeyDown={handleKeyDown} />
    {expanded && <div className="lab-combobox-menu" id={`${id}-options`} role="listbox" aria-label={`${label} options`}>
      {filtered.map((option, index) => <button key={option.value} id={`${id}-${index}`} type="button"
        className={`lab-combobox-option${index === active ? ' active' : ''}`} role="option" aria-selected={option.value === value}
        ref={index === active ? activeRef : undefined} tabIndex={-1}
        onMouseDown={event => event.preventDefault()} onClick={() => select(option.value)}>{option.label}</button>)}
      {!filtered.length && <p className="combobox-empty" role="status">No matching options.</p>}
    </div>}
  </div>;
}
