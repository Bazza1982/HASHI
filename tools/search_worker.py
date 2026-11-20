"""One managed foreground scan. No service, ports, background job or index."""
from __future__ import annotations

import fnmatch
from functools import cache
import json
import stat
import os
import re
import sys
import time
from pathlib import Path

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.literal_search import scan_literal

PROJECT_EXCLUDED_DIRS = frozenset({'.git', 'node_modules', '.venv', '.venv-wsl',
                                  'venv', '__pycache__', '.tox', '.cache', 'target',
                                  'dist', 'build', '.pytest_cache', '.mypy_cache', 'workspaces'})
PROJECT_EXCLUDED_FILES = frozenset({'tool_ledger.jsonl', 'tool_action_audit.jsonl', 'checkpoints.jsonl'})
CONTAINER_SUFFIXES = frozenset({'.pdf', '.docx', '.xlsx', '.pptx', '.zip', '.gz', '.bz2',
                              '.xz', '.7z', '.tar', '.png', '.jpg', '.jpeg', '.gif',
                              '.webp', '.mp4', '.mp3', '.wav', '.sqlite', '.db', '.pyc',
                              '.exe', '.dll', '.so'})


def glob_match(path: str, pattern: str, sensitive: bool) -> bool:
    """Root-relative glob: * is one segment, ** zero or more segments."""
    if not sensitive:
        path, pattern = path.casefold(), pattern.casefold()
    if '/' not in pattern:
        return fnmatch.fnmatchcase(path.rsplit('/', 1)[-1], pattern)
    parts, pats = path.split('/'), pattern.split('/')
    @cache
    def match(i, j):
        if j == len(pats):
            return i == len(parts)
        if pats[j] == '**':
            return any(match(k, j + 1) for k in range(i, len(parts) + 1))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], pats[j]) and match(i + 1, j + 1)
    return match(0, 0)


def signature(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


class StaleCursor(Exception):
    pass


class Scanner:
    def __init__(self, config: dict):
        self.cfg = config
        self.state = config.get('state') or {
            'roots': [{'path': item['path'], 'stack': [], 'pending_file': None,
                       'initialized': False, 'done': False, 'failed': False}
                      for item in config['selected_roots']],
            'counters': {'directories_enumerated': 0, 'files_enumerated': 0,
                         'text_files_checked': 0, 'characters_read': 0, 'bytes_read': None,
                         'matches': 0, 'skipped': 0, 'errors': 0},
            'errors': [], 'warnings': [], 'turn': 0,
        }
        self.count = self.state['counters']
        self.matches = []
        self.match_chars = 0
        self.last_progress = time.monotonic()
        self.stop_reason = 'completed'
        # A deadline stop must never produce a zero-progress continuation:
        # each invocation performs at least one batch before yielding.
        self.worked = False
        # Resolve trusted boundaries once; frames carry the physical prefix
        # through links so ordinary descendants need no per-file resolve().
        self.read_roots = [Path(value).resolve() for value in config['authorized_roots']]
        self.blocked = [Path(value).resolve() for value in config.get('protected_read_paths', [])]
        self.selected = [Path(item['path']).resolve() for item in config['selected_roots']]
        self.iterators: dict[str, object] = {}

    def error(self, path, code):
        self.count['errors'] += 1
        if len(self.state['errors']) < 12:
            self.state['errors'].append({'path': str(path)[:512], 'code': code})

    def authorized(self, path: Path, resolved: Path | None = None) -> bool:
        # The caller supplies the physical path derived from its parent frame.
        # Resolve only roots, links and legacy continuation state.
        if resolved is None:
            resolved = path.resolve()
        return (any(resolved == root or resolved.is_relative_to(root) for root in self.read_roots)
                and any(resolved == root or resolved.is_relative_to(root) for root in self.selected)
                and not any(resolved == root or resolved.is_relative_to(root) for root in self.blocked))

    def ignored(self, relative: str, name: str, directory: bool):
        if not self.cfg['include_hidden'] and name.startswith('.'):
            return True
        if self.cfg['profile'] == 'project':
            if directory and name in PROJECT_EXCLUDED_DIRS or name in PROJECT_EXCLUDED_FILES:
                return True
        if any(glob_match(relative, value, self.cfg['case_sensitive'])
               for value in self.cfg['exclude']):
            return True
        return False

    def selected_candidate(self, relative):
        include = self.cfg['include']
        return not include or any(glob_match(relative, value, self.cfg['case_sensitive']) for value in include)

    def _regex_pattern(self):
        pattern = getattr(self, '_compiled_regex', None)
        if pattern is None:
            flags = 0 if self.cfg['case_sensitive'] else re.IGNORECASE
            pattern = re.compile(self.cfg['query'], flags)
            self._compiled_regex = pattern
        return pattern

    def path_matches(self, relative):
        query = self.cfg['query']
        if self.cfg['match'] == 'glob':
            return glob_match(relative, query, self.cfg['case_sensitive'])
        if self.cfg['match'] == 'regex':
            return self._regex_pattern().search(relative) is not None
        return query in relative if self.cfg['case_sensitive'] else query.casefold() in relative.casefold()

    def add(self, match):
        # Pending matches must remain tied to the object actually observed,
        # including when an output-budget stop happens at a file boundary.
        observed = signature(Path(match['path']))
        if match.get('file_signature') and match['file_signature'] != observed:
            raise StaleCursor(match['path'])
        match['file_signature'] = observed
        if match['type'] == 'file':
            match['size_bytes'] = observed[2]
        size = len(json.dumps(match, ensure_ascii=False))
        if size > self.cfg['match_budget']:
            self.error(match['path'], 'match_exceeds_output_budget')
            return True
        if self.matches and self.match_chars + size > self.cfg['match_budget']:
            self.state['pending_match'] = match
            self.stop_reason = 'output_budget'
            return False
        self.matches.append(match)
        self.match_chars += size
        self.count['matches'] += 1
        if len(self.matches) == 1:
            self.progress(force=True)
        if len(self.matches) >= self.cfg['max_results']:
            self.stop_reason = 'page_limit'
            return False
        return True

    def progress(self, *, force=False):
        if force or time.monotonic() - self.last_progress >= 1:
            self.last_progress = time.monotonic()
            print(json.dumps({'kind': 'progress', 'counters': self.count,
                              'matches': self.matches,
                              'coverage': {'roots': [{'path': root['path'], 'exhausted': root['done'],
                                                      'failed': root['failed']} for root in self.state['roots']]}}), flush=True)

    def frame(self, path, *, sig=None, resolved=None):
        frame = {'path': str(path), 'offset': 0,
                 'signature': sig if sig is not None else signature(path)}
        if resolved is not None:
            frame['resolved'] = resolved
        return frame

    def iterator(self, frame):
        name = frame['path']
        if name not in self.iterators:
            iterator = os.scandir(name)
            if not frame.get('enumerated'):
                self.count['directories_enumerated'] += 1
                frame['enumerated'] = True
            for _ in range(frame['offset']):
                next(iterator)
            self.iterators[name] = iterator
        return self.iterators[name]

    def initialize(self, root):
        path = Path(root['path'])
        selected = next(item for item in self.cfg['selected_roots'] if item['path'] == root['path'])
        if selected.get('available') is False:
            self.error(path, 'root_unavailable')
            root.update(done=True, failed=True, initialized=True)
            return
        if not path.exists():
            self.error(path, 'root_unavailable')
            root.update(done=True, failed=True, initialized=True)
            return
        root['signature'] = signature(path)
        root['initialized'] = True
        if path.is_dir():
            root['stack'].append(self.frame(path, resolved=str(path.resolve())))
        else:
            if self.ignored(path.name, path.name, False) or not self.selected_candidate(path.name):
                self.count['skipped'] += 1
                root['done'] = True
                return
            root['pending_file'] = {'path': str(path), 'relative': path.name,
                                    'signature': signature(path), 'text_state': None}
            self.count['files_enumerated'] += 1

    def text_batch(self, root):
        item = root['pending_file']
        path = Path(item['path'])
        physical = Path(item['resolved']) if item.get('resolved') else path.resolve()
        if not self.authorized(path, resolved=physical):
            self.error(path, 'permission_denied')
            root['pending_file'] = None
            return True
        if signature(path) != item['signature']:
            raise StaleCursor(str(path))
        if self.cfg['mode'] == 'path':
            root['pending_file'] = None
            if self.path_matches(item['relative']) and self.selected_candidate(item['relative']):
                return self.add({'path': str(path), 'type': 'file', 'root': root['path']})
            return True
        if not item['text_state'] and self.cfg['operation'] != 'log_query':
            if path.suffix.casefold() in CONTAINER_SUFFIXES:
                self.count['skipped'] += 1
                root['pending_file'] = None
                return True
            with physical.open('rb') as probe:
                if b'\0' in probe.read(8192):
                    self.count['skipped'] += 1
                    root['pending_file'] = None
                    return True
        if not item['text_state']:
            self.count['text_files_checked'] += 1
        before = self.count['characters_read']
        def progress(count):
            self.count['characters_read'] = before + count
            self.progress()
        result = scan_literal(physical, terms=tuple(self.cfg.get('terms') or [self.cfg['query']]),
                              case_sensitive=self.cfg['case_sensitive'],
                              regex=self.cfg['match'] == 'regex',
                              max_results=max(1, self.cfg['max_results'] - len(self.matches)),
                              context_chars=self.cfg['context_chars'], state=item['text_state'],
                              character_budget=128 * 1024, progress=progress)
        item['text_state'] = result['continuation']
        if result['encoding_warning'] and 'UTF-8 replacement characters observed' not in self.state['warnings']:
            self.state['warnings'].append('UTF-8 replacement characters observed')
        if result.get('coverage_warning') and result['coverage_warning'] not in self.state['warnings']:
            self.state['warnings'].append(result['coverage_warning'])
        if signature(path) != item['signature']:
            raise StaleCursor(str(path))
        # A bounded queue preserves every unreturned match if serialization stops.
        pending = [{**match, 'path': str(path), 'type': 'file', 'root': root['path']}
                   for match in result['matches']]
        while pending:
            match = pending.pop(0)
            if not self.add(match):
                self.state['pending_matches'] = pending
                if item['text_state'] is None:
                    root['pending_file'] = None
                return False
        if item['text_state'] is None:
            root['pending_file'] = None
        return True

    def batch(self, root):
        if not root['initialized']:
            self.initialize(root)
        if root['done']:
            return True
        if root['pending_file']:
            return self.text_batch(root)
        for _ in range(64):
            if not root['stack']:
                root['done'] = True
                return True
            frame = root['stack'][-1]
            directory = Path(frame['path'])
            iterator = self.iterator(frame)
            entry = next(iterator, None)
            if entry is None:
                iterator.close()
                self.iterators.pop(frame['path'], None)
                root['stack'].pop()
                continue
            frame['offset'] += 1
            path = Path(entry.path)
            relative = path.relative_to(Path(root['path'])).as_posix()
            linked = entry.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())
            if linked and not self.cfg['follow_links']:
                self.count['skipped'] += 1
                continue
            try:
                # One metadata call per entry; DirEntry.stat reuses the open
                # directory handle instead of re-resolving the path each time.
                st = entry.stat(follow_symlinks=self.cfg['follow_links'])
            except OSError:
                self.count['skipped'] += 1
                continue
            directory_entry = stat.S_ISDIR(st.st_mode)
            if self.ignored(relative, entry.name, directory_entry):
                self.count['skipped'] += 1
                continue
            # Ordinary children inherit their parent's physical prefix, even
            # several levels below a link/junction. Resolve actual links only.
            if 'resolved' not in frame:
                frame['resolved'] = str(directory.resolve())
            target = path.resolve() if linked else Path(frame['resolved']) / entry.name
            if not self.authorized(path, resolved=target):
                self.error(path, 'permission_denied')
                continue
            if directory_entry:
                # Every frame, including the root, participates in cycles.
                if linked and any(frame2.get('resolved') is not None
                                   and Path(frame2['resolved']) == target
                                   for frame2 in root['stack']):
                    self.error(path, 'link_cycle')
                    continue
                if target in self.selected and target != Path(root['stack'][0]['resolved']):
                    continue  # nested selected root is scheduled independently
                # Frame signatures use path.stat: DirEntry.stat snapshots can
                # lag by nanoseconds and report st_dev=0 on Windows, which
                # would falsely invalidate continuations or skip every
                # directory as a foreign filesystem.
                sig = signature(path)
                if not self.cfg['cross_filesystems'] and sig[0] != root['signature'][0]:
                    self.count['skipped'] += 1
                    continue
                if len(root['stack']) >= 128:
                    self.error(path, 'depth_budget')
                    continue
                root['stack'].append(self.frame(
                    path, sig=sig, resolved=str(target)))
                if self.cfg['mode'] == 'path' and self.path_matches(relative) and self.selected_candidate(relative):
                    if not self.add({'path': str(path), 'type': 'dir', 'root': root['path']}):
                        return False
                if not self.cfg['recursive']:
                    root['stack'].pop()
                continue
            if not stat.S_ISREG(st.st_mode):
                self.count['skipped'] += 1
                continue
            self.count['files_enumerated'] += 1
            if not self.selected_candidate(relative):
                self.count['skipped'] += 1
                continue
            root['pending_file'] = {'path': str(path), 'relative': relative,
                                    'signature': signature(path),
                                    'text_state': None, 'resolved': str(target)}
            if not self.text_batch(root):
                return False
            if self.cfg['mode'] == 'content':
                # Keep tight interruption granularity for body scans.
                return True
            # Path mode: continue the bounded batch so deadline checks stay
            # useful without paying a loop iteration per file.
        return True

    def run(self):
        try:
            for root in self.state['roots']:
                if root.get('initialized') and not root['done']:
                    if signature(Path(root['path'])) != root['signature']:
                        raise StaleCursor(root['path'])
                    for frame in root['stack']:
                        if signature(Path(frame['path'])) != frame['signature']:
                            raise StaleCursor(frame['path'])
                        physical = Path(frame['path']).resolve()
                        if frame.get('resolved') and physical != Path(frame['resolved']):
                            raise StaleCursor(frame['path'])
                        frame['resolved'] = str(physical)
            pending = ([self.state.pop('pending_match')] if 'pending_match' in self.state else [])
            pending += self.state.pop('pending_matches', [])
            while pending:
                match = pending.pop(0)
                if not self.add(match):
                    self.state['pending_matches'] = pending
                    return self.result()
            roots = self.state['roots']
            while any(not root['done'] for root in roots):
                self.progress()
                if self.cfg.get('deadline') and time.time() >= self.cfg['deadline'] and self.worked:
                    self.stop_reason = 'deadline'
                    break
                root = roots[self.state['turn'] % len(roots)]
                self.state['turn'] += 1
                self.worked = True
                try:
                    if not self.batch(root):
                        break
                except OSError as exc:
                    self.error(root['path'], type(exc).__name__)
                    root.update(done=True, failed=True)
            return self.result()
        except StaleCursor:
            self.stop_reason = 'cursor_stale'
            self.error('continuation', 'file_tree_changed')
            return self.result(stale=True)
        finally:
            for iterator in self.iterators.values():
                iterator.close()

    def result(self, *, stale=False):
        warnings = list(self.state['warnings'])
        if self.stop_reason == 'deadline':
            # The deadline notice belongs to this round's result only; it must
            # not persist into continuation state and falsify the completeness
            # of a later fully-resumed scan.
            message = 'Scan stopped before completing coverage'
            if message not in warnings:
                warnings.append(message)
        exhausted = (all(root['done'] for root in self.state['roots']) and
                     not self.state.get('pending_match') and not self.state.get('pending_matches'))
        complete = exhausted and not self.count['errors'] and not warnings and not stale
        if self.stop_reason == 'completed' and self.count['errors']:
            self.stop_reason = 'io_error'
        state = None if exhausted or stale else self.state
        if state and len(json.dumps(state, ensure_ascii=False).encode('utf-8')) > 2500000:
            state = None
            self.state['warnings'].append('Continuation capacity exceeded; no scan continues after return')
        self.progress(force=True)
        return {'matches': self.matches, 'counters': self.count,
                'coverage': {'roots': [{'path': root['path'], 'exhausted': root['done'],
                                       'failed': root['failed']} for root in self.state['roots']],
                             'errors': self.state['errors']},
                'scope_exhausted': exhausted, 'coverage_complete': complete,
                'stop_reason': self.stop_reason, 'warnings': warnings,
                'state': state}


def main():
    # Test-only fixture: lets a parent simulate an unresponsive worker to
    # exercise the emergency cleanup path. Never set in production scans.
    hang = os.environ.get('HASHI_SEARCH_WORKER_TEST_HANG_SECONDS')
    if hang:
        try:
            time.sleep(float(hang))
        except (TypeError, ValueError):
            pass
    # The managed pipe protocol is UTF-8, independent of Windows ANSI locale
    # or inherited PYTHONIOENCODING. Do not reconfigure the importing parent.
    sys.stdout.reconfigure(encoding='utf-8', errors='strict')
    sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
    config = json.loads(sys.stdin.buffer.readline(4 * 1024 * 1024))
    result = Scanner(config).run()
    print(json.dumps({'kind': 'result', 'data': result}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
