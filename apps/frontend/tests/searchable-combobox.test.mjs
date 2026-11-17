import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import * as jsxRuntime from 'react/jsx-runtime';

const compiled = ts.transpileModule(readFileSync(new URL('../src/SearchableCombobox.tsx', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
}).outputText;

function harness(options, value = options[0]?.value || '') {
  let cursor = 0, dirty = true, tree, effects = [];
  const slots = [], changes = [], component = {};
  const hooks = {
    useId() { return `combo-${cursor++}`; },
    useState(initial) { const id = cursor++; slots[id] ??= initial;
      return [slots[id], next => { next = typeof next === 'function' ? next(slots[id]) : next; dirty ||= !Object.is(slots[id], next); slots[id] = next; }]; },
    useRef(initial) { const id = cursor++; return slots[id] ||= { current: initial }; },
    useEffect(callback, deps) { const id = cursor++, old = slots[id];
      if (!old || deps.some((value, index) => !Object.is(value, old[index]))) effects.push(() => { slots[id] = deps; callback(); }); },
  };
  new Function('exports', 'require', compiled)(component, name => name === 'react' ? hooks : jsxRuntime);
  const props = { label: 'Administrator', value, options, onChange(value) { changes.push(value); props.value = value; dirty = true; } };
  const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])];
  const find = (type, match = () => true) => { const node = nodes(tree).find(node => node?.type === type && match(node.props)); assert.ok(node, `Missing ${type}`); return node; };
  function render() {
    for (let n = 0; dirty && n < 20; n++) {
      cursor = 0; dirty = false; effects = []; tree = component.SearchableCombobox(props);
      effects.forEach(effect => effect());
    }
    assert.equal(dirty, false);
  }
  const act = action => { action(); dirty = true; render(); };
  const input = () => find('input');
  render();
  return { props, changes, find, input, act, options: () => nodes(tree).filter(node => node?.props?.role === 'option'),
    search: value => act(() => input().props.onChange({ target: { value } })),
    key: key => { let prevented = false, stopped = false;
      act(() => input().props.onKeyDown({ key, preventDefault() { prevented = true; }, stopPropagation() { stopped = true; } }));
      return { prevented, stopped }; } };
}

test('search handles city names, offsets and administrator emails, and accepts only listed options', () => {
  const zones = harness([
    { value: 'America/Bogota', label: 'America/Bogota (UTC-05:00)' },
    { value: 'America/New_York', label: 'America/New York (UTC-04:00)' },
    { value: 'UTC', label: 'UTC (UTC+00:00)' },
  ]);
  zones.act(() => zones.input().props.onFocus()); zones.key('ArrowUp');
  assert.equal(zones.input().props['aria-activedescendant'], zones.options().at(-1).props.id);
  zones.key('ArrowDown'); assert.equal(zones.input().props['aria-activedescendant'], zones.options().at(-1).props.id);
  zones.key('ArrowUp'); assert.equal(zones.input().props['aria-activedescendant'], zones.options()[1].props.id);
  zones.search('NEW_yORK'); assert.equal(zones.options().length, 1);
  zones.key('Escape'); assert.match(zones.input().props.value, /Bogota/);
  zones.search('not-a-zone'); zones.key('Enter');
  assert.equal(zones.options().length, 0); assert.equal(zones.input().props['aria-activedescendant'], undefined);
  assert.equal(zones.find('p', props => props.role === 'status').props.children, 'No matching options.');
  zones.key('Tab'); assert.match(zones.input().props.value, /Bogota/); assert.deepEqual(zones.changes, []);
  zones.search(' utc+00:00 '); zones.key('Enter'); assert.deepEqual(zones.changes, ['UTC']);
  assert.equal(zones.input().props.value, 'UTC (UTC+00:00)');
  const admins = harness([{ value: 'operator', label: 'JOEL_admin@example.test' }, { value: 'other', label: 'other@example.test' }], '');
  admins.search(' joel_ADMIN '); assert.equal(admins.options().length, 1);
  admins.act(() => admins.options()[0].props.onClick());
  assert.deepEqual(admins.changes, ['operator']); assert.equal(admins.input().props.value, 'JOEL_admin@example.test');
});

test('Escape closes the list before the containing dialog, and blur preserves the selection', () => {
  const view = harness([{ value: 'operator', label: 'operator@example.test' }]);
  view.search('other'); assert.deepEqual(view.key('Escape'), { prevented: true, stopped: true });
  assert.equal(view.input().props['aria-expanded'], false); assert.deepEqual(view.key('Escape'), { prevented: false, stopped: false });
  view.act(() => view.input().props.onClick());
  view.act(() => view.find('div', props => props.className.includes('searchable-combobox')).props.onBlur({ currentTarget: { contains: () => false }, relatedTarget: null }));
  assert.equal(view.input().props['aria-expanded'], false); assert.equal(view.input().props.value, 'operator@example.test');
  assert.deepEqual(view.changes, []);
});

test('disabling an open picker hides its options and blocks selection, and removed options cannot be selected', () => {
  const view = harness([{ value: 'operator', label: 'operator@example.test' }]);
  view.search('operator');
  view.act(() => { view.props.disabled = true; });
  assert.equal(view.input().props.disabled, true); assert.equal(view.options().length, 0);
  view.search('operator'); view.key('Enter'); assert.deepEqual(view.changes, []);
  view.act(() => { view.props.disabled = false; view.props.options = []; });
  view.search('operator'); view.key('Enter'); assert.deepEqual(view.changes, []);
  assert.equal(view.input().props['aria-activedescendant'], undefined);
});
