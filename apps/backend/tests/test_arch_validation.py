"""Native wrapper failures must stay failures, including Sentrux's exit-zero drift."""
import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[3]


def test_rules_use_supported_constraints_without_line_limits():
    rules = tomllib.loads((ROOT / '.sentrux/rules.toml').read_text(encoding='utf-8'))
    # Sentrux 0.5.7 silently ignores unknown keys; detect a misspelled constraint.
    assert set(rules) == {'constraints'}
    assert set(rules['constraints']) == {'max_cycles', 'no_god_files'}
    assert rules['constraints']['max_cycles'] == 0
    assert rules['constraints']['no_god_files'] is True


@pytest.mark.parametrize('case', ['large_agent', 'cycle', 'fan_out'])
def test_native_rules_allow_long_agents_but_reject_dependency_regressions(tmp_path, case):
    scanner = shutil.which('sentrux')
    if scanner is None:
        pytest.skip('Sentrux CLI is required for native architecture policy acceptance')
    rules = ROOT / '.sentrux/rules.toml'
    constraints = tomllib.loads(rules.read_text(encoding='utf-8'))['constraints']
    assert 'max_file_lines' not in constraints
    assert 'max_fn_lines' not in constraints
    target = tmp_path / '.sentrux/rules.toml'
    target.parent.mkdir()
    shutil.copyfile(rules, target)
    if case == 'large_agent':
        (tmp_path / 'agent.py').write_text(
            'PROMPT = """\n' + 'Research instructions.\n' * 10000 + '"""\n'
            + 'def answer():\n    return PROMPT\n', encoding='utf-8')
    elif case == 'cycle':
        (tmp_path / 'first.py').write_text('import second\ndef first():\n    return second.second()\n', encoding='utf-8')
        (tmp_path / 'second.py').write_text('import first\ndef second():\n    return first.first()\n', encoding='utf-8')
    else:
        (tmp_path / 'agent.py').write_text(
            '\n'.join(f'import tool_{index}' for index in range(20)), encoding='utf-8')
        for index in range(20):
            (tmp_path / f'tool_{index}.py').write_text('def tool():\n    return 1\n', encoding='utf-8')
    result = subprocess.run([scanner, 'check', str(tmp_path)], capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=30)
    output = result.stdout + result.stderr
    assert '2 rules checked' in output, output
    if case == 'large_agent':
        assert result.returncode == 0, output
    else:
        assert result.returncode != 0, output
        assert ('max_cycles' if case == 'cycle' else 'no_god_files') in output, output


@pytest.fixture
def scope_helper():
    spec = importlib.util.spec_from_file_location('sentrux_scope', ROOT / 'scripts/sentrux_scope.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_snapshot_includes_new_files_and_excludes_private_or_generated_trees(tmp_path, monkeypatch, scope_helper):
    root, target = tmp_path / 'repo', tmp_path / 'snapshot'
    paths = ['tracked.py', 'nuevo con espacio.py', '.aidp/private.py', '.source/old.py',
             'node_modules/vendor.js', 'apps/viewer/.upstream/built.js', '.sentrux/baseline.json']
    for name in paths:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding='utf-8')

    def git(arguments, **kwargs):
        assert arguments == ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard']
        assert kwargs['cwd'] == root
        return SimpleNamespace(stdout='\0'.join(paths + ['deleted.py']).encode())

    monkeypatch.setattr(scope_helper.subprocess, 'run', git)
    assert scope_helper.snapshot(root, target) == 3
    assert sorted(path.relative_to(target).as_posix() for path in target.rglob('*') if path.is_file()) == [
        '.sentrux/baseline.json', 'nuevo con espacio.py', 'tracked.py']
    assert (root / '.sentrux/baseline.json').read_bytes() == (target / '.sentrux/baseline.json').read_bytes()


@pytest.mark.parametrize('path', ['../outside.py', '/outside.py', 'dir\\outside.py'])
def test_snapshot_rejects_paths_outside_its_source_scope(tmp_path, monkeypatch, scope_helper, path):
    monkeypatch.setattr(scope_helper.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=path.encode()))
    with pytest.raises(ValueError, match='leaves the repository'):
        scope_helper.snapshot(tmp_path, tmp_path / 'target')


def test_snapshot_does_not_follow_an_internal_link_into_an_excluded_tree(tmp_path, monkeypatch, scope_helper):
    root = tmp_path / 'repo'
    private = root / '.aidp/private.py'
    private.parent.mkdir(parents=True)
    private.write_text('private runtime code', encoding='utf-8')
    public = root / 'public.py'
    public.write_text('link entry', encoding='utf-8')
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, 'resolve', lambda self, *args, **kwargs:
                        private if self == public else original_resolve(self, *args, **kwargs))
    monkeypatch.setattr(scope_helper.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=b'public.py'))
    assert scope_helper.snapshot(root, tmp_path / 'snapshot') == 0
    assert not (tmp_path / 'snapshot/public.py').exists()


def test_scope_runner_never_initializes_over_an_existing_reference(tmp_path, monkeypatch, scope_helper):
    baseline = tmp_path / '.sentrux/baseline.json'
    baseline.parent.mkdir()
    baseline.write_bytes(b'preserve the historical reference')
    monkeypatch.setattr(scope_helper.shutil, 'which', lambda _: 'sentrux')
    with pytest.raises(ValueError, match='existing references are preserved'):
        scope_helper.run(tmp_path, 'gate', save=True)
    assert baseline.read_bytes() == b'preserve the historical reference'


def test_scope_runner_propagates_degradation_even_with_a_zero_exit(tmp_path, monkeypatch, scope_helper):
    monkeypatch.setattr(scope_helper.shutil, 'which', lambda _: 'sentrux')
    monkeypatch.setattr(scope_helper, 'snapshot', lambda *args: 1)
    monkeypatch.setattr(scope_helper.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(
        stdout='DEGRADED', stderr='', returncode=0))
    assert scope_helper.run(tmp_path, 'gate') == 1


@pytest.mark.parametrize('script', ['arch-preflight.ps1', 'arch-postflight.ps1'])
@pytest.mark.parametrize('output,exit_code,failed', [('DEGRADED', 0, True), ('failed', 1, True), ('PASSED', 0, False)])
def test_arch_wrappers_preserve_the_reference_and_do_not_hide_failed_gates(script, output, exit_code, failed):
    shell = shutil.which('pwsh')
    if shell is None:
        pytest.skip('PowerShell is required to exercise the native validation wrappers')
    baseline = ROOT / '.sentrux/baseline.json'
    before = hashlib.sha256(baseline.read_bytes()).hexdigest()
    path = str(ROOT / 'scripts' / script).replace("'", "''")
    command = ("function global:sentrux { }; function global:graphify { $global:LASTEXITCODE = 0 }; "
               + "function global:python { $global:LASTEXITCODE = " + str(exit_code)
               + "; Write-Output '" + output + "' }; & '" + path + "'")
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert (result.returncode != 0) == failed, result.stdout + result.stderr
    assert hashlib.sha256(baseline.read_bytes()).hexdigest() == before
    if failed:
        assert 'complete.' not in result.stdout
    else:
        phase = 'preflight' if script == 'arch-preflight.ps1' else 'postflight'
        assert 'Architecture ' + phase + ' complete.' in result.stdout


@pytest.mark.parametrize('failure', ['update', 'export'])
def test_postflight_stops_when_graph_refresh_or_wiki_export_fails(tmp_path, failure):
    shell = shutil.which('pwsh')
    if shell is None:
        pytest.skip('PowerShell is required to exercise the native validation wrappers')
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    shutil.copyfile(ROOT / 'scripts/arch-postflight.ps1', scripts / 'arch-postflight.ps1')
    (tmp_path / 'graphify-out').mkdir()
    command = ("function global:graphify { if ($args[0] -eq '" + failure
               + "') { $global:LASTEXITCODE = 1 } else { $global:LASTEXITCODE = 0 } }; "
               + "function global:sentrux { }; function global:python { throw 'Gate must not run after Graphify fails' }; & '"
               + str(scripts / 'arch-postflight.ps1').replace("'", "''") + "'")
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert result.returncode != 0
    assert ('Graphify update failed.' if failure == 'update' else 'Graphify wiki export failed.') in result.stderr
    assert 'Gate must not run' not in result.stderr
