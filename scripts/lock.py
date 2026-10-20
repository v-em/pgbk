"""Freeze installed runtime dependency closure; excludes test/emulator packages."""
from importlib import metadata
from pathlib import Path
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

queue = ["boto3", "psycopg", "psycopg-binary", "jsonschema", "pyyaml", "pglast", "kubernetes"]
resolved = {}
while queue:
    name = canonicalize_name(queue.pop())
    if name in resolved:
        continue
    package = metadata.distribution(name)
    resolved[name] = package.version
    for item in package.requires or []:
        requirement = Requirement(item)
        if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
            queue.append(requirement.name)
Path("requirements.lock").write_text("# Runtime dependency closure; regenerate with scripts/lock.py\n" +
                                     "".join(f"{name}=={version}\n" for name, version in sorted(resolved.items())))
