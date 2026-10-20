"""Check translation pairs, requirement/test IDs, examples and local links."""
import re
from pathlib import Path

for en in Path("docs/en").glob("*.md"):
    ru = Path("docs/ru") / en.name
    assert ru.is_file(), f"Missing Russian translation: {en}"
    a, b = en.read_text(), ru.read_text()
    assert set(re.findall(r"(?:REQ|T)-\d{3}", a)) == set(re.findall(r"(?:REQ|T)-\d{3}", b)), en
    def examples(text):
        return [(language, code) for language, code in re.findall(r"```([^\n]*)\n(.*?)```", text, re.S) if language != "mermaid"]
    assert examples(a) == examples(b), en
for path in [Path("README.md"), Path("README.ru.md"), *Path("docs").rglob("*.md")]:
    for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
        if "://" not in target and not target.startswith("#"):
            assert (path.parent / target.split("#")[0]).exists(), f"Broken link: {path}: {target}"
print("Bilingual documentation and local links passed")
