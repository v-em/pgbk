import hashlib
import io
from types import SimpleNamespace

import pytest
from botocore.exceptions import ResponseStreamingError
from pgbk.errors import Error
from pgbk.process import Control
from pgbk.storage import S3


class Broken(io.BytesIO):
    def read(self, size=-1):
        if self.tell():
            raise ResponseStreamingError(error='synthetic dropped body')
        return super().read(2)


def adapter(bodies):
    s3=S3.__new__(S3)
    s3.control=Control()
    s3.control.stop.wait=lambda duration: None
    s3.bucket='fixture'
    remaining=iter(bodies)
    s3.client=SimpleNamespace(get_object=lambda **kwargs: {'Body':next(remaining)})
    return s3


def test_stream_retry_restarts_hash_and_closes_failed_body():
    broken=Broken(b'prefix-not-an-artifact')
    s3=adapter([broken,io.BytesIO(b'complete')])
    assert s3.remote_hash('object')==(hashlib.sha256(b'complete').hexdigest(),8)
    assert broken.closed


def test_stream_retry_is_bounded():
    s3=adapter([Broken(b'partial') for _ in range(5)])
    with pytest.raises(Error) as error:
        s3.remote_hash('object')
    assert error.value.code=='S3_RETRY_EXHAUSTED'
