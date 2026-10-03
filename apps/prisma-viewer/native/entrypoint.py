"""Run the private native server and AIDP bridge as one rollback-compatible image."""
from pathlib import Path
import signal
import subprocess
import sys
import time


def supervise(commands):
    children = []
    stopping = False

    def stop(_number, _frame):
        nonlocal stopping
        stopping = True

    old_handlers = {number: signal.signal(number, stop) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        for command in commands:
            children.append(subprocess.Popen(command))
        while not stopping:
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
