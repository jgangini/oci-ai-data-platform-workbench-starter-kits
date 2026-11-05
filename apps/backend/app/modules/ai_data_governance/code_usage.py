"""Re-admit stored source references with the current caller before model use."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
from threading import Event
from urllib.parse import quote
from uuid import UUID

from .code_context import source_references, workspace_code_path, workspace_job_name, WorkspaceCodeReader
from .model_output import model_json


async def current_code_references(caller, workspace, rows, *, verifier=None):
    """Discover current static references; historical evidence is not re-versioned."""
    if getattr(caller, "code_reads_enabled", False) is not True:
        raise PermissionError("Current source discovery has not been admitted")
    cancelled = Event()
    def check_active():
        if cancelled.is_set():
            raise InterruptedError("Current source discovery was cancelled")
        caller.check_identity()
    def request(method, path, **kwargs):
        check_active()
        result = caller(method, path, **kwargs)
        check_active()
        return result
    request.check_identity = check_active
    request.code_reads_enabled = True
    def scan():
        reader = WorkspaceCodeReader(request, workspace, check_active=check_active)
        admitted = verifier or CallerCodeVerifier(request, workspace, check_active=check_active)
        found = []
        for row in rows:
            check_active()
            refs = reader.find(row["table_key"], row["column_name"])
            verified = [admitted.admit(ref, row) for ref in refs]
            found.append((row, [ref for ref in verified if ref is not None]))
        return found
    check_active()
    try:
        result = await asyncio.to_thread(scan)
    except asyncio.CancelledError:
        cancelled.set()
        raise
    check_active()
    return result


def _usage_reference(ref, row):
    fields = {'table_key', 'column_name', 'cell', 'line', 'operation', 'source_path',
              'source_hash', 'job_key', 'task_key', 'execution_evidence'}
    if not isinstance(ref, dict) or set(ref) - {'job_name'} != fields:
        raise ValueError('Invalid stored source reference')
    if 'job_name' in ref and workspace_job_name({'name': ref['job_name']}) is None:
        raise ValueError('Invalid stored workflow name')
    if (ref['table_key'] != row['table_key'] or ref['column_name'] != row['column_name']
            or ref['execution_evidence'] is not False):
        raise ValueError('Source reference belongs to another target')
    workspace_code_path(ref['source_path'])
    if not isinstance(ref['source_hash'], str) or not re.fullmatch(r'[0-9a-f]{64}', ref['source_hash']):
        raise ValueError('Invalid stored source hash')
    if not isinstance(ref['job_key'], str) or str(UUID(ref['job_key'])) != ref['job_key']:
        raise ValueError('Invalid stored job identity')
    if not isinstance(ref['task_key'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,127}', ref['task_key']):
        raise ValueError('Invalid stored task identity')
    _reference_locator(ref, row)
    return ref


def _reference_locator(ref, row):
    if type(ref['line']) is not int or not 1 <= ref['line'] <= 2097152:
        raise ValueError('Invalid source line')
    if ref['cell'] is not None and (type(ref['cell']) is not int or not 1 <= ref['cell'] <= 1000):
        raise ValueError('Invalid notebook cell')
    expected = ('literal_spark_table_reference', 'literal_sql_create_table_reference') if row['column_name'] is None else ('literal_column_select', 'literal_column_groupBy')
    if ref['operation'] not in expected:
        raise ValueError('Invalid source operation')



def _usage_time(value):
    if not isinstance(value, str):
        raise ValueError('A timestamped source observation is required')
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.tzinfo is None:
        raise ValueError('Source observation must have a timezone')
    return at


def usage_candidate(candidate, revisions):
    fields = {'evidence_id', 'scope_id', 'version_id', 'kind', 'facts_json', 'coverage', 'observed_at', 'expires_at'}
    if not isinstance(candidate, dict) or set(candidate) != fields or candidate['kind'] != 'STATIC_CODE':
        raise ValueError('Invalid stored static evidence')
    row = revisions.get((candidate['scope_id'], candidate['version_id']))
    if row is None or candidate['coverage'] != 'PARTIAL':
        raise ValueError('Static evidence is not an admitted partial observation')
    if not isinstance(candidate['evidence_id'], str) or not re.fullmatch(r'[0-9a-f]{64}', candidate['evidence_id']):
        raise ValueError('Invalid source evidence identity')
    now = datetime.now(timezone.utc)
    if not _usage_time(candidate['observed_at']) <= now < _usage_time(candidate['expires_at']):
        return None
    refs = _candidate_references(candidate, row)
    return row, [_usage_reference(ref, row) for ref in refs]


def _candidate_references(candidate, row):
    stored = model_json(candidate['facts_json'], max_bytes=65536)
    if set(stored) != {'usage_context', 'observation_run_id'}:
        raise ValueError('Invalid source evidence provenance')
    usage = stored['usage_context']
    if (not isinstance(usage, dict) or set(usage) != {'target', 'references', 'execution_evidence', 'analysis_complete'}
            or usage['target'] != row['qualified_name'] or usage['execution_evidence'] is not False
            or usage['analysis_complete'] is not False):
        raise ValueError('Invalid static usage context')
    refs = usage['references']
    if not isinstance(refs, list) or not 1 <= len(refs) <= 16:
        raise ValueError('Stored source references exceed their budget')
    return refs


class CallerCodeVerifier:
    """One verification phase; denied responses and raw code never leave this object."""

    def __init__(self, caller, workspace, *, check_active=None):
        if getattr(caller, 'code_reads_enabled', False) is not True:
            raise PermissionError('Caller source reads have not been admitted')
        self.caller, self.workspace = caller, workspace
        self.check_active = check_active or caller.check_identity
        self.jobs, self.sources, self.total_bytes = {}, {}, 0

    def _read(self, path):
        self.check_active()
        try:
            value = self.caller('GET', path)[0]
        except (PermissionError, RuntimeError, ValueError, UnicodeError):
            value = None
        self.check_active()
        return value

    def _job(self, key):
        if key not in self.jobs:
            if len(self.jobs) >= 16:
                return None
            value = self._read(f'/workspaces/{self.workspace}/jobs/{key}')
            self.jobs[key] = value if isinstance(value, dict) and value.get('key') == key else None
        return self.jobs[key]

    def _source(self, path):
        if path not in self.sources:
            if len(self.sources) >= 16 or self.total_bytes >= 8 * 1024 * 1024:
                return None
            self.sources[path] = None
            source = self._read(f'/workspaces/{self.workspace}/objects/' + quote(path, safe=''))
            if not isinstance(source, str):
                return None
            self.total_bytes += len(source.encode())
            if self.total_bytes > 8 * 1024 * 1024:
                return None
            try:
                self.sources[path] = source_references(path, source)
            except (ValueError, RecursionError):
                return None
        return self.sources[path]

    def verify(self, ref, row):
        _usage_reference(ref, row)
        job = self._job(ref['job_key'])
        if not self._task_matches(job, ref):
            return False
        observed = self._source(ref['source_path'])
        if observed is None or observed['source_hash'] != ref['source_hash']:
            return False
        core = {k: ref[k] for k in ('table_key', 'column_name', 'cell', 'line', 'operation')}
        return core in observed['references']

    def admit(self, ref, row):
        if not self.verify(ref, row):
            return None
        name = workspace_job_name(self.jobs.get(ref['job_key']))
        return {**ref, **({'job_name': name} if name is not None else {})}

    def _task_matches(self, job, ref):
        if not isinstance(job, dict) or not isinstance(job.get('tasks'), list) or len(job['tasks']) > 64:
            return False
        if 'job_name' in ref and ref['job_name'] != workspace_job_name(job):
            return False
        matches = [task for task in job['tasks'] if isinstance(task, dict) and task.get('taskKey') == ref['task_key']]
        if len(matches) != 1:
            return False
        task = matches[0]
        field = 'filePath' if task.get('type') == 'PYTHON_TASK' else 'notebookPath' if task.get('type') == 'NOTEBOOK_TASK' else None
        return field is not None and task.get('source') == 'WORKSPACE' and task.get(field) == ref['source_path']
