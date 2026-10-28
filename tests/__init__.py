import sys

# Plain `python -m unittest` walks the repo root and imports tagteam.harnesses before any test, which pins
# state.HOME to the real ~/.tagteam; every test after that writes there. Refuse to run instead.
if "tagteam.state" in sys.modules:
    raise RuntimeError("tagteam was imported before the tests could isolate ~/.tagteam; "
                       "run `python3 -m unittest discover -s tests -t .`")
