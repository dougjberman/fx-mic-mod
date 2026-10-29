"""Copy firmware/ to docs/firmware/ for the install page, without comments or docstrings.

The mic's drive is small and recorded takes need most of it, so the copies the
site installs carry no comments or docstrings. Each one keeps its line numbers,
so a traceback from the mic still points at the right line of firmware/.

Run: python3 tools/slim_firmware.py
"""

import io
import os
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRMWARE = os.path.join(ROOT, "firmware")
SITE = os.path.join(ROOT, "docs", "firmware")

_STATEMENT_START = (tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, None)


def slim(source):
    """``source`` with comments and docstrings removed, line for line.

    The SPDX license lines stay.
    """
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def offset(pos):
        return starts[pos[0] - 1] + pos[1]

    cuts = []  # (start, end, replacement)
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    previous = None
    for index, token in enumerate(tokens):
        kind = token.type
        if kind == tokenize.COMMENT and "SPDX-" not in token.string:
            cuts.append((offset(token.start), offset(token.end), ""))
        elif kind == tokenize.STRING and previous in _STATEMENT_START:
            following = next(
                (t.type for t in tokens[index + 1:] if t.type not in (tokenize.COMMENT, tokenize.NL)),
                None)
            if following in (tokenize.NEWLINE, tokenize.ENDMARKER):
                # A bare string statement: keep a placeholder, so a block whose
                # only statement was its docstring still has a body.
                cuts.append((offset(token.start), offset(token.end), "0" + "\n" * token.string.count("\n")))
        if kind not in (tokenize.COMMENT, tokenize.NL):
            previous = kind

    out = []
    at = 0
    for start, end, replacement in cuts:
        out.append(source[at:start])
        out.append(replacement)
        at = end
    out.append(source[at:])
    return "\n".join(line.rstrip() for line in "".join(out).split("\n"))


def site_copy(name):
    """The bytes docs/firmware/``name`` should hold."""
    with open(os.path.join(FIRMWARE, name), "rb") as file:
        data = file.read()
    if name.endswith(".py"):
        data = slim(data.decode("utf-8")).encode("utf-8")
    return data


def site_names():
    return sorted(n for n in os.listdir(FIRMWARE) if n.endswith((".py", ".json")))


def main():
    os.makedirs(SITE, exist_ok=True)
    for name in os.listdir(SITE):
        if name not in site_names():
            os.remove(os.path.join(SITE, name))
    before = after = 0
    for name in site_names():
        data = site_copy(name)
        with open(os.path.join(SITE, name), "wb") as file:
            file.write(data)
        before += os.path.getsize(os.path.join(FIRMWARE, name))
        after += len(data)
    print("docs/firmware: {} KB, from {} KB".format(after // 1024, before // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
