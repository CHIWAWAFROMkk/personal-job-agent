"""Run two real native desktop processes against a synthetic temporary workspace.

No cloud keys, personal data, existing desktop window, or system association is used.
The executable performs WebView2 DOM interactions and writes explicit coverage.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def find_latest_packaged_exe() -> Path:
    candidates = list((ROOT / 'dist/desktop').glob('*/PersonalJobAgent/PersonalJobAgent.exe'))
    if not candidates:
        raise FileNotFoundError('No packaged desktop executable; specify --exe.')
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def isolated_environment(temp_root: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('JOB_AGENT_') and key not in {
               'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'DEEPSEEK_API_KEY', 'BOCHA_API_KEY',
               'BRAVE_SEARCH_API_KEY', 'AMAP_WEB_API_KEY', 'AMAP_API_KEY'}}
    env.update(TEMP=str(temp_root), TMP=str(temp_root),
               PYTHONPATH=os.pathsep.join((str(ROOT / 'src'), str(ROOT))),
               JOB_AGENT_AI_PROVIDER='local', JOB_AGENT_SEARCH_PROVIDER='none',
               JOB_AGENT_MAP_PROVIDER='none')
    return env


def run_native_verification(command: list[str], qa_dir: Path) -> dict:
    qa_dir.mkdir(parents=True, exist_ok=True)
    temp_root = qa_dir / 'temp'
    temp_root.mkdir(exist_ok=True)
    data_root = Path(tempfile.mkdtemp(prefix='window-', dir=temp_root))
    env = isolated_environment(temp_root)
    result = {'status': 'failed', 'data_root': str(data_root), 'phases': {},
              'coverage': 'Two independent native WebView2 processes; visible DOM click flow and restart persistence'}
    for phase, flag in (('first_run', '--window-test'), ('restart', '--window-restart-test')):
        report = qa_dir / f'{data_root.name}-{phase}.json'
        log = qa_dir / f'{data_root.name}-{phase}.log'
        args = command + [flag, '--data-dir', str(data_root), '--window-report', str(report)]
        with log.open('w', encoding='utf-8') as stream:
            proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                returncode = proc.wait(timeout=100)
            except subprocess.TimeoutExpired:
                # This is our own isolated child and its WebView2 processes only.
                if os.name == 'nt':
                    subprocess.run(['taskkill.exe', '/PID', str(proc.pid), '/T', '/F'],
                                   stdout=stream, stderr=subprocess.STDOUT, timeout=10,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    proc.kill()
                proc.wait(timeout=10)
                result['error'] = f'{phase} exceeded 100 seconds; owned process tree stopped'
                break
        details = json.loads(report.read_text(encoding='utf-8')) if report.exists() else {'status': 'failed', 'error': 'No report'}
        result['phases'][phase] = {'exit_code': returncode, 'report': str(report), 'log': str(log), **details}
        if returncode or details.get('status') != 'ok':
            break
    if len(result['phases']) == 2 and all(p['status'] == 'ok' and not p['exit_code'] for p in result['phases'].values()):
        result['status'] = 'ok'
    target = qa_dir / 'native-desktop-verification.json'
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"{result['status']}: {target}", flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument('--exe', type=Path)
    choice.add_argument('--source-python', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'work/product-desktop/native-qa')
    args = parser.parse_args()
    command = ([str(args.source_python.resolve()), '-m', 'job_agent.desktop'] if args.source_python
               else [str((args.exe or find_latest_packaged_exe()).resolve())])
    return 0 if run_native_verification(command, args.output.resolve())['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
