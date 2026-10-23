"""Run tools/ting_test.py's automatic suite against the fakes, through a
pretend raw REPL, and print the report.

    python3 tests/run_selftest.py <root dir> <results dir>

The fake mic hears a steady tone rather than its own speaker, so the
loopback check fails and the audio checks are skipped; everything else
(the REPL protocol, uploads, selftest.py's records, the report) runs for
real.
"""

import contextlib
import io
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "firmware"))
sys.path.insert(0, os.path.join(REPO, "tools"))

import fakes  # noqa: E402


class FakeLink:
    """Answers like CircuitPython's raw REPL, running code in this process."""

    def __init__(self):
        self.out = b""
        self.code = b""
        self.raw = False
        self.scope = {}
        self.closed = False

    def write(self, data):
        for byte in data:
            ch = bytes([byte])
            if ch == b"\x01":
                self.raw = True
                self.out += b"raw REPL; CTRL-B to exit\r\n>"
            elif ch == b"\x02":
                self.raw = False
            elif not self.raw:
                continue
            elif ch == b"\x04":
                self._run(self.code.decode())
                self.code = b""
            else:
                self.code += ch

    def _run(self, code):
        stdout, stderr = io.StringIO(), ""
        try:
            with contextlib.redirect_stdout(stdout):
                exec(code, self.scope)
        except Exception:
            stderr = traceback.format_exc()
        self.out += b"OK" + stdout.getvalue().replace("\n", "\r\n").encode() + b"\x04"
        self.out += stderr.encode() + b"\x04>"

    def read(self, size):
        data, self.out = self.out[:size], self.out[size:]
        return data

    def reset_input_buffer(self):
        self.out = b""

    def close(self):
        self.closed = True


def main():
    root, results = sys.argv[1], sys.argv[2]
    fakes.install(fakes.Script(end=1e12), root)
    import selftest
    # The fake clock only moves when the fake mic records, which keeps
    # each run short; the CPU spin has nothing to measure here.
    selftest._spin = lambda ms: 1000
    selftest.now_ms = lambda: int(fakes.SCRIPT.clock.t * 1000)
    import ting_test
    real_init = ting_test.RawRepl.__init__

    def init(self, port, timeout=10.0, link=None):
        real_init(self, port, timeout, link=FakeLink())

    ting_test.RawRepl.__init__ = init
    status = ting_test.main(["--port", "fake", "check", "--out", results, "--stress", "2"])
    print("STATUS", status)


main()
