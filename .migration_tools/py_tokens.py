"""Rewrite colour literals inside triple-quoted QSS strings in python files."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import qss_tokens as q

TRIPLE = re.compile(r'(?P<prefix>[fFrRbBuU]{0,2})"""(?P<body>.*?)"""', re.S)
COLOR = re.compile(r"#[0-9a-fA-F]{6}\b|rgba?\(\s*\d")

for arg in sys.argv[1:]:
    path = Path(arg)
    text = path.read_text(encoding="utf-8")
    skipped = []

    def sub(m):
        body = m.group("body")
        if not COLOR.search(body):
            return m.group(0)
        if "f" in m.group("prefix").lower():
            skipped.append(body[:60])
            return m.group(0)
        return m.group("prefix") + '"""' + q.convert_qss(body) + '"""'

    new = TRIPLE.sub(sub, text)
    if new != text:
        path.write_bytes(new.encode("utf-8"))
    remaining = [(i + 1, l.strip()) for i, l in enumerate(new.splitlines()) if COLOR.search(l)]
    print(f"== {path}")
    for s in skipped:
        print("  SKIPPED f-string:", s.replace("\n", " "))
    for ln, l in remaining:
        print(f"  {ln}: {l}")

print("UNMAPPED:", sorted(q.unmapped))
for d in q.decisions:
    print("DECISION:", d)
