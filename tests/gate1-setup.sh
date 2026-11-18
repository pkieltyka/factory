#!/usr/bin/env bash
# Gate 1 fixture: a small repo left mid-task, with no team running and no session cache.
# Usage: gate1-setup.sh <factory skill dir> [parent dir]     (each run builds a new directory)
set -euo pipefail
SKILL=$(cd "$1" && pwd)
GATE=$(mktemp -d "${2:-${TMPDIR:-/tmp}}/factory-gate1.XXXXXX")
cd "$GATE"
git init -q -b main
g() { git -c user.name=gate1 -c user.email=gate1@example.invalid -c commit.gpgsign=false "$@"; }

cat > README.md <<'EOF'
# slug

Turns text into URL slugs. Tests: `python3 -m unittest`.
EOF
cat > slug.py <<'EOF'
def slugify(text):
    return text.lower()
EOF
cat > test_slug.py <<'EOF'
import unittest

from slug import slugify


class SlugTests(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(slugify("Hello"), "hello")


if __name__ == "__main__":
    unittest.main()
EOF
python3 "$SKILL/scripts/factory.py" init --cwd "$GATE" > /dev/null
python3 - <<'EOF'
import pathlib, re
p = pathlib.Path("FACTORY.md")
project = "slug: a one-function Python library that turns text into URL slugs. Standard library only. Tests: `python3 -m unittest`."
text, n = re.subn(r"(## Project\n\n).*?(?=## )", lambda m: m.group(1) + project + "\n\n", p.read_text(), count=1, flags=re.S)
assert n == 1
p.write_text(text)
EOF
cp "$SKILL/templates/TODO.md" TODO.md
g add -A
g commit -q -m "Start slug"

# T1: done, reviewed and committed.
cat > slug.py <<'EOF'
import re


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
EOF
cat > test_slug.py <<'EOF'
import unittest

from slug import slugify


class SlugTests(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(slugify("Hello"), "hello")

    def test_joins_words(self):
        self.assertEqual(slugify("Hello, World"), "hello-world")


if __name__ == "__main__":
    unittest.main()
EOF
g add -A
g commit -q -m "T1: slugify joins words with hyphens"
T1=$(git rev-parse --short HEAD)

# The tracker as the team left it. T4 is the planted contradiction: no such commit, no such code.
cat > TODO.md <<EOF
TODO
====

## Ready

- [ ] T3 Command line -- python3 -m slug prints the slug
  - owner: none yet
  - criteria: \`python3 -m slug "Hello World"\` prints \`hello-world\`.
  - next: start after T2 is done; it changes the same function.

## In progress

- [ ] T2 max_length -- limit the slug length (@lead-dev)
  - criteria: \`slugify(text, max_length=n)\` returns at most n characters and never ends with \`-\`; without max_length nothing changes; tests cover it.
  - work: uncommitted in the main checkout: \`slug.py\`, \`test_slug_length.py\`
  - verdict: changes required by @reviewer on those paths; ran \`python3 -m unittest\`, 3 passed
  - next: @lead-dev makes the required fixes, then @reviewer re-reviews:
    1. \`slug.py:8\`: \`slugify("hello world", max_length=6)\` returns \`hello-\`; strip the trailing \`-\` after cutting.
    2. Add a test where max_length cuts right after a word.

## Backlog

## Done

- [x] T1 Slugify joins words -- join words with hyphens ($T1)
  - owner: @lead-dev
  - criteria: \`slugify("Hello, World")\` returns \`hello-world\`; tests pass.
  - verdict: approve by @reviewer on $T1; ran \`python3 -m unittest\`, 2 passed
  - next: none

- [x] T4 Strip accents -- remove accents from the slug (0f3a9c1)
  - owner: @lead-dev
  - criteria: \`slugify("Crème")\` returns \`creme\`.
  - verdict: approve by @reviewer on 0f3a9c1; ran \`python3 -m unittest\`
  - next: none
EOF
g add TODO.md
g commit -q -m "Tracker"

# T2: reviewed, changes required, never committed.
cat > slug.py <<'EOF'
import re


def slugify(text, max_length=None):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if max_length is None:
        return slug
    return slug[:max_length]
EOF
cat > test_slug_length.py <<'EOF'
import unittest

from slug import slugify


class LengthTests(unittest.TestCase):
    def test_cuts_to_max_length(self):
        self.assertEqual(slugify("hello world", max_length=8), "hello-wo")


if __name__ == "__main__":
    unittest.main()
EOF

# A change no task explains.
printf '\nReleases are cut from the `stable` branch every Friday.\n' >> README.md

echo "Gate 1 fixture ready: $GATE (T1 is commit $T1)"
git status --short
