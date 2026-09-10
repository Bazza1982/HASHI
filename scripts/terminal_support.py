"""Terminal presentation and read-only diagnostics; lifecycle stays in the instance CLI."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from contextvars import ContextVar

_RESULT = ContextVar("terminal_result", default=None)

def record_effect(step, *, unknown=False):
    current=_RESULT.get()
    if current is not None:
        current["unknown" if unknown else "steps"].append(step)

def record_instance(name):
    current=_RESULT.get()
    if current is not None:
        current["instance"] = name


def _safe(value):
    from orchestrator.bootstrap_logging import redact_log_text
    return redact_log_text(value)


def invoke(run, argv, usage_error):
    context={'steps':[], 'unknown':[], 'instance':None}
    _RESULT.set(context)
    as_json = '--json' in argv and argv[:1] != ['--complete']
    # Streaming is intentionally not captured into an unbounded StringIO.
    follow = argv[:1] != ['--complete'] and 'logs' in argv and any(x in argv for x in ('-f', '--follow'))
    if follow:
        try:
            return run(argv)
        except KeyboardInterrupt:
            return 130
        except usage_error as exc:
            print('INVALID_ARGUMENT: ' + _safe(exc), file=sys.stderr)
            return 64
    from scripts.terminal_language import resolve, localize
    language = resolve(argv)
    live = not as_json and sys.stdout.isatty()
    out, err = io.StringIO(), io.StringIO()
    class LiveOutput:
        def write(self, value):
            out.write(value)
            return actual_stdout.write(localize(value, language))
        def flush(self):
            actual_stdout.flush()
        def fileno(self):
            return actual_stdout.fileno()
        def isatty(self):
            return actual_stdout.isatty()
    actual_stdout = sys.stdout
    try:
        with redirect_stdout(LiveOutput() if live else out), redirect_stderr(err):
            result = run(argv)
    except usage_error as exc:
        result = 64
        err.write('INVALID_ARGUMENT: ' + str(exc))
    except KeyboardInterrupt:
        result = 130
        err.write('INTERRUPTED: Command interrupted; inspect status before retrying lifecycle commands.')
    except PermissionError:
        result = 77
        err.write('PERMISSION_DENIED: Check local file permissions.')
    except OSError as exc:
        result = 74
        err.write('IO_ERROR: ' + type(exc).__name__)
    except Exception as exc:
        result = 70
        err.write('INTERNAL_ERROR: ' + type(exc).__name__ + '; inspect hashi doctor.')
    except SystemExit as exc:
        result = int(exc.code or 0)
    stdout, stderr = _safe(out.getvalue()), _safe(err.getvalue())
    from scripts.terminal_language import resolve, localize
    language = resolve(argv)
    codes = {64:'INVALID_ARGUMENT',66:'INSTANCE_NOT_FOUND',69:'SERVICE_UNAVAILABLE',70:'INTERNAL_ERROR',74:'IO_ERROR',75:'INSTANCE_BUSY',77:'PERMISSION_DENIED',78:'RUNTIME_INCOMPLETE',130:'INTERRUPTED'}
    code = codes.get(result, 'INTERNAL_ERROR')
    for known in ('START_TIMEOUT','STOP_TIMEOUT','ONBOARDING_REQUIRED','UPDATE_PENDING','ACTIVITY_UNVERIFIED','PORT_CONFLICT','CONFIRMATION_REQUIRED','PURGE_DENIED'):
        if known in stderr:
            code = known
    if 'Unknown HASHI instance:' in stderr:
        code, result = 'INSTANCE_NOT_FOUND', 66
    if 'No HASHI instance' in stderr:
        code = 'INSTANCE_REQUIRED'
    if 'ambiguous' in stderr.lower() or 'Multiple HASHI' in stderr:
        code = 'INSTANCE_AMBIGUOUS'
    if 'argument command: invalid choice' in stderr:
        code = 'UNKNOWN_COMMAND'
    if 'unrecognized arguments' in stderr and result == 64:
        code = 'INVALID_ARGUMENT'
    categories = (
        ('Permanent purge requires', 'CONFIRMATION_REQUIRED', 77),
        ('Refusing to purge external', 'PURGE_DENIED', 77),
        ('Managed data path is not', 'PURGE_DENIED', 77),
        ('Managed data marker does not match', 'PURGE_DENIED', 77),
        ('cannot launch a WSL checkout', 'ENVIRONMENT_MISMATCH', 78),
        ('cannot adopt a Windows executable', 'ENVIRONMENT_MISMATCH', 78),
        ('No approved CPython', 'RUNTIME_INCOMPLETE', 78),
        ('has not adopted it', 'UPDATE_PENDING', 78),
        ('must explicitly adopt program', 'UPDATE_PENDING', 78),
        ('could not be verified', 'ACTIVITY_UNVERIFIED', 75),
        ('cannot be verified through the local API', 'ACTIVITY_UNVERIFIED', 75),
    )
    for fragment, category, exit_code in categories:
        if result and fragment in stderr:
            code, result = category, exit_code
            break
    effects = {'steps': list(context['steps']), 'unknown': list(context['unknown'])}
    if 'Start was sent' in stderr:
        effects['steps'] = list(dict.fromkeys([*effects['steps'], 'start_requested']))
        effects['unknown'].append('readiness')
    if 'Shutdown was accepted' in stderr:
        effects['steps'].append('stop_accepted')
        effects['unknown'].append('stopped')
        code = 'STOP_TIMEOUT'
    if as_json:
        try:
            data = json.loads(stdout) if stdout.strip() else None
        except ValueError:
            data = {'message': stdout.strip()}
        print(json.dumps({'schema_version':1, 'ok':result == 0, 'command':_command(argv),
                          'instance':context['instance'] or _instance(argv), 'exit_code':result, 'data':data, 'warnings':[],
                          'error':None if result == 0 else {'code':code,'message':stderr.strip(), 'next_step':'hashi help; hashi status; hashi doctor'},
                          'effects':effects}, ensure_ascii=False))
    else:
        if stdout and not live:
            print(localize(stdout, language), end='')
        if stderr:
            prefix = '' if stderr.startswith(code + ':') else code + ': '
            print(localize(prefix + stderr.rstrip() + '\nNext: hashi help; hashi status; hashi doctor', language), file=sys.stderr)
    return result


def _instance(argv):
    for i, value in enumerate(argv):
        if value in ('--instance','-i') and i+1 < len(argv):
            return argv[i+1]
        if value.startswith('--instance='):
            return value.split('=',1)[1]
    return None


def _command(argv):
    skip = False
    for value in argv:
        if skip:
            skip = False
            continue
        if value in ('--instance','-i','--lang'):
            skip = True
        elif not value.startswith('-'):
            return value
    return 'tui'


def doctor(registry, inspect):
    entries = []
    for folder in os.get_exec_path():
        for suffix in ('.ps1','.cmd','.exe','') if os.name == 'nt' else ('',):
            path = Path(folder) / ('hashi' + suffix)
            if path.is_file() and str(path) not in entries:
                entries.append(str(path))
    from scripts.hashi_instance_cli import _select_runtime
    try:
        runtime = _select_runtime(registry.program_root, full=True)
        missing = []
    except Exception as exc:
        runtime = None
        missing = [type(exc).__name__ + ': approved runtime/dependencies unavailable']
    return {'environment':registry.environment_id, 'entry':str(registry.program_root / 'cli.js'),
            'path_entries':entries,'runtime':runtime,'missing':missing,
            'instances':[inspect(r, registry.resolved_code_root(r)) for r in registry.records()],
            'effects':[]}


def stream_logs(record, root, args):
    home = Path(record['bridge_home'])
    if args.agent:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',args.agent):
            from scripts.hashi_instance_cli import TerminalUsageError
            raise TerminalUsageError('Invalid agent ID')
        config = json.loads((home / 'agents.json').read_text(encoding='utf-8-sig'))
        agents = config.get('agents',{})
        names = set(agents) if isinstance(agents,dict) else {a.get('name') for a in agents}
        if args.agent not in names:
            from scripts.hashi_instance_cli import TerminalUsageError
            raise TerminalUsageError('Unknown agent ID')
    path = home / 'logs' / 'bridge.log'
    if not path.is_file():
        path = home / 'logs' / 'hashi-cli.log'
    if not path.is_file():
        print('No runtime log exists.')
        return 0
    def filtered(line):
        if args.agent and not re.search(r'(?<![\w-])' + re.escape(args.agent) + r'(?![\w-])',line):
            return
        line = _safe(line.rstrip())
        # Do not project content-bearing diagnostic records into ordinary logs.
        if re.search(r'(?i)(system.prompt|transcript|chat.content|request.body|response.body)',line):
            line = '[content record omitted]'
        return line
    def emit(line):
        line = filtered(line)
        if line is None:
            return
        if args.json:
            print(json.dumps({'schema_version':1,'ok':True,'command':'logs','instance':record['name'], 'data':{'line':line}},ensure_ascii=False),flush=True)
        else:
            print(line,flush=True)
    with path.open(encoding='utf-8', errors='replace') as handle:
        tail = deque(handle,maxlen=args.lines)
        if args.json and not args.follow:
            print(json.dumps({'lines':[value for x in tail if (value := filtered(x)) is not None]},ensure_ascii=False))
        else:
            for line in tail:
                emit(line)
        while args.follow:
            line = handle.readline()
            if line:
                emit(line)
            else:
                time.sleep(.2)
                if path.stat().st_size < handle.tell():
                    handle.seek(0)
    return 0


def completion_candidates(parser, words):
    tokens = list(words)
    prefix = tokens.pop() if tokens else ""
    root_options = {option:action for action in parser._actions for option in action.option_strings}
    selected = parser
    expecting = None
    for token in tokens:
        if expecting is not None:
            expecting = None
            continue
        options = {**root_options, **{option:action for action in selected._actions for option in action.option_strings}}
        option = options.get(token.split("=", 1)[0])
        if option is not None:
            if option.nargs != 0 and "=" not in token:
                expecting = option
            continue
        sub = next((action for action in selected._actions if isinstance(action, argparse._SubParsersAction)), None)
        if sub is not None and token in sub.choices:
            selected = sub.choices[token]
    if expecting is not None:
        values = [str(value) for value in (expecting.choices or [])]
    else:
        values = list(root_options)
        for action in selected._actions:
            if action.help == argparse.SUPPRESS:
                continue
            values.extend(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                values.extend(action.choices)
    return sorted({value for value in values if value != "--complete" and value.startswith(prefix)})


def completion_script(parser, shell):
    # Query the actual parser at completion time; no copied command/flag table.
    if shell == 'bash':
        return '''_hashi_complete() {
  local choices
  choices=$(hashi --complete "${COMP_WORDS[@]:1:$COMP_CWORD}" 2>/dev/null)
  COMPREPLY=($(compgen -W "$choices" -- "${COMP_WORDS[COMP_CWORD]}"))
}
complete -F _hashi_complete hashi'''
    if shell == 'zsh':
        return '''#compdef hashi
_hashi_complete() {
  local -a matches
  matches=("${(@f)$(hashi --complete "${words[@]:1:$((CURRENT-1))}" 2>/dev/null)}")
  compadd -a matches
}
compdef _hashi_complete hashi'''
    if shell == 'fish':
        return '''function __hashi_complete
  set -l parts (commandline -opc)
  hashi --complete $parts[2..-1] (commandline -ct) 2>/dev/null
end
complete -c hashi -f -a '(__hashi_complete)'
'''
    return '''Register-ArgumentCompleter -Native -CommandName hashi -ScriptBlock {
  param($wordToComplete, $commandAst, $cursorPosition)
  $parts = @($commandAst.CommandElements | Select-Object -Skip 1 | ForEach-Object { $_.Extent.Text })
  if (-not $wordToComplete) { $parts += '' }
  & hashi --complete @parts 2>$null
}'''
