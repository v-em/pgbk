import sys

import pytest

from pgbk.errors import Error
from pgbk.process import Control


@pytest.mark.parametrize("streaming", [False, True])
def test_access_denied_is_classified_without_echoing_native_stderr(streaming):
    argv = [sys.executable, "-c", "import sys; sys.stderr.write('permission denied for table secret_canary_sql'); sys.exit(1)"]
    with pytest.raises(Error) as failure:
        if streaming:
            with Control(5).output(argv) as chunks:
                list(chunks)
        else:
            Control(5).run(argv)
    assert failure.value.code == "ACCESS_DENIED"
    assert failure.value.details["toolExitCode"] == 1
    assert "secret_canary_sql" not in str(failure.value.report())


def test_streamed_stderr_backpressure_does_not_deadlock_or_corrupt_archive():
    argv = [sys.executable, "-c", r"""import os
os.write(1, b'PGDMP')
for _ in range(256):
    os.write(2, b'secret_diagnostic' * 512)
os.write(1, b'\x00archive_payload')
"""]
    with Control(5).output(argv) as chunks:
        assert b"".join(chunks) == b"PGDMP\x00archive_payload"
