"""Retain installed worker license/copyright files as a tar stream on stdout."""
import sys
import tarfile
from pathlib import Path

paths = set()
for root in [Path('/usr/share/doc'), Path('/usr/share/common-licenses'),
             Path('/usr/local/share/pgbk/licenses')]:
    if root.exists():
        paths.update(p for p in root.rglob('*') if p.is_file())
for root in Path('/opt/pgbk/lib').glob('python*/site-packages'):
    for metadata in root.glob('*.dist-info'):
        paths.update(p for p in metadata.rglob('*') if p.is_file() and
                     (p.name == 'METADATA' or 'license' in str(p).lower() or
                      p.name.lower().startswith(('copying', 'copyright', 'notice'))))
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz', dereference=True) as archive:
    for path in sorted(paths):
        archive.add(path, arcname=str(path).lstrip('/'), recursive=False)
