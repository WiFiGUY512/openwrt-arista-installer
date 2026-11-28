"""Checks for how a run ends on an error it did not expect.  No AP and no network.

    python test-errors.py

A download that breaks off is the network's doing, not the tool's, and has to
say so: a release fetch stopped once on "IncompleteRead" and was reported as
"a fault in the tool" (2026-11-23); the same command worked at once.
"""
import http.client
import importlib.util
import os
import sys
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("af", os.path.join(HERE, "arista-field.py"))
af = importlib.util.module_from_spec(spec)
sys.modules["af"] = af
spec.loader.exec_module(af)

FAILED = []


def check(name, ok, detail=""):
    print("%s  %s%s" % ("ok  " if ok else "FAIL", name, ("   (%s)" % (detail,)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


class Died(Exception):
    pass


def ending(exc):
    """What run_main() says when main() raises exc."""
    said = []

    def die(msg, hint=None):
        said.append(str(msg))
        raise Died()

    def main():
        raise exc

    saved = (af.main, af.die)
    af.main, af.die = main, die
    try:
        af.run_main()
    except Died:
        pass
    finally:
        af.main, af.die = saved
    return said[0] if said else ""


msg = ending(http.client.IncompleteRead(b"x" * 10, 20))
check("a download that breaks off: says so, and to run it again; not a fault in the tool",
      "download was interrupted" in msg and "run the tool again" in msg and "fault in the tool" not in msg, msg)
msg = ending(urllib.error.URLError("no route to host"))
check("no connection at all: the same", "download was interrupted or could not be made" in msg, msg)
msg = ending(TimeoutError("timed out"))
check("a timeout: the same", "download was interrupted" in msg, msg)
msg = ending(urllib.error.HTTPError("https://example.invalid/x", 404, "Not Found", {}, None))
check("an HTTP error answer: names the code and the address, and says to open an issue if it stays",
      "GitHub answered 404" in msg and "example.invalid" in msg and "open an issue" in msg, msg)
msg = ending(KeyError("board"))
check("anything else is still reported as the tool's own fault, with the log",
      "fault in the tool" in msg and "KeyError" in msg, msg)

print("")
print("%d failed" % len(FAILED) if FAILED else "all error-ending checks pass")
sys.exit(1 if FAILED else 0)
