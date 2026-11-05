"""Run the private native server and AIDP bridge as one rollback-compatible image."""
from pathlib import Path
import signal
import subprocess
import sys
import time

from cryptography.exceptions import InvalidTag
from provider_runtime import environment, file_signature, stored


def supervise(commands):
    children = []
    stopping = False

    def stop(_number, _frame):
        nonlocal stopping
        stopping = True

    old_handlers = {number: signal.signal(number, stop) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        signature = file_signature()
        failed_signature = None
        storage_unavailable = False
        for index, command in enumerate(commands):
            children.append(subprocess.Popen(command, env=environment(stored()) if index == 0 else None))
        while not stopping:
            try:
                changed = file_signature()
                storage_unavailable = False
            except OSError:
                if not storage_unavailable:
                    print('Provider settings storage is unavailable; keeping the active worker', file=sys.stderr)
                storage_unavailable = True
                changed = signature
            if changed != signature:
                try:
                    updated = environment(stored())
                except (OSError, ValueError, TypeError, InvalidTag):
                    if changed != failed_signature:
                        print('Provider settings reload failed; keeping the active worker', file=sys.stderr)
                    failed_signature = changed
                else:
                    children[0].terminate()
                    try:
                        children[0].wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        children[0].kill()
                        children[0].wait()
                    children[0] = subprocess.Popen(commands[0], env=updated)
                    signature = changed
                    failed_signature = None
            if any(child.poll() is not None for child in children):
                return 1  # Either process exiting, even cleanly, leaves an incomplete viewer.
            time.sleep(0.2)
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 10
        for child in children:
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for number, handler in old_handlers.items():
            signal.signal(number, handler)


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    Path('/tmp/gev-logs').mkdir(exist_ok=True)
    sys.exit(supervise([['node', str(root / 'native/runtime.mjs')], [sys.executable, str(root / 'server.py')]]))
