#!/usr/bin/python3 -I
"""Require firewall initialization before applying the one-way module lock."""
from pathlib import Path
import subprocess
import sys
import time


def wait_ready(binary=Path('/usr/bin/firewall-cmd'), timeout=60, interval=1):
    if not binary.is_file():
        return True  # Platforms without firewalld have no firewall startup to wait for.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = subprocess.run([str(binary), '--state'], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    timeout=min(5, max(0.01, deadline - time.monotonic())))
            if result.returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(interval)
    return False


if __name__ == '__main__':
    if not wait_ready():
        print('Firewall initialization did not finish; kernel module loading remains unlocked', file=sys.stderr)
        sys.exit(1)
