#!/usr/bin/python3
"""Adversarial test host: deliberately forwards unsigned/tampered envelopes."""
import json
from pathlib import Path
import select
import struct
import sys
Path('/run/stopscroll/fake-host-used').touch()
try:
    while True:
        try:
            value = json.loads(Path('/run/stopscroll/browser.json').read_text())
        except (OSError, ValueError):
            value = None
        payload = json.dumps(value).encode()
        sys.stdout.buffer.write(struct.pack('=I', len(payload)) + payload)
        sys.stdout.buffer.flush()
        if select.select([sys.stdin.buffer], [], [], .2)[0]:
            break
except (OSError, BrokenPipeError):
    pass
