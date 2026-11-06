"""Check translation pairs, requirement/test IDs, examples and local links."""
import re
import json
import tomllib
from pathlib import Path

import yaml

project = tomllib.loads(Path("pyproject.toml").read_text())
lock = tomllib.loads(Path("uv.lock").read_text())
versions = {
    "project": project["project"]["version"],
    "runtime": re.search(r'__version__ = "([^"]+)"', Path("src/pgbk/__init__.py").read_text())[1],
    "chart": yaml.safe_load(Path("charts/pgbk/Chart.yaml").read_text())["version"],
    "appVersion": yaml.safe_load(Path("charts/pgbk/Chart.yaml").read_text())["appVersion"],
    "lock": next(p["version"] for p in lock["package"] if p["name"] == "pgbk"),
    "compatibility": json.loads(Path("docs/compatibility.json").read_text())["version"],
}
assert len(set(versions.values())) == 1, f"Inconsistent release versions: {versions}"

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
