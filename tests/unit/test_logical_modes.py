import hashlib
import io
import sys
import tarfile
from types import SimpleNamespace

import pytest

from pgbk.config import normalize
from pgbk.errors import Error
from pgbk.process import Control
from pgbk.restore import logical_archive
from pgbk.storage import S3


@pytest.mark.parametrize("options,major,code", [
    ({"jobs": 2},17,"PARALLEL_FORMAT_REQUIRED"),
    ({"format":"directory","jobs":2,"transfer":{"mode":"direct"}},17,"DIRECT_FORMAT_REQUIRED"),
    ({"compression":{"method":"zstd"}},14,"COMPRESSION_UNSUPPORTED"),
    ({"compression":{"method":"gzip","level":10}},17,"COMPRESSION_LEVEL_INVALID"),
    ({"compression":{"method":"lz4","level":1}},17,"COMPRESSION_LEVEL_INVALID"),
    ({"compression":{"method":"none","level":0}},17,"COMPRESSION_LEVEL_INVALID"),
    ({"compression":{"method":"zstd","level":23}},17,"COMPRESSION_LEVEL_INVALID"),
])
def test_invalid_modes(values, options, major, code):
    values["profiles"]["example-logical"]["logical"].update(options)
    values["targets"]["example"]["serverMajor"]=major
    with pytest.raises(Error) as failure: normalize(values)
    assert failure.value.code==code


def adapter():
    s3=object.__new__(S3)
    s3.control=Control()
    s3.bucket="fixture"
    s3.calls=[]
    s3.parts=[]
    def call(operation, **kw):
        s3.calls.append(operation)
        if operation=="create_multipart_upload": return {"UploadId":"one"}
        if operation=="upload_part":
            s3.parts.append(kw["Body"])
            return {"ETag":str(len(s3.parts))}
        if operation=="complete_multipart_upload": return {}
    s3.call=call
    s3.client=SimpleNamespace(abort_multipart_upload=lambda **kw:s3.calls.append("abort"))
    return s3


def test_stream_parts_exact_and_bounded():
    s3=adapter()
    content=b"abcdefghijk"*100
    digest,size=s3.upload_stream("key",iter([content[i:i+17] for i in range(0,len(content),17)]),64)
    assert b"".join(s3.parts)==content
    assert all(len(p)==64 for p in s3.parts[:-1])
    assert (digest,size)==(hashlib.sha256(content).hexdigest(),len(content))
    assert s3.calls[-1]=="complete_multipart_upload"


def test_nonzero_producer_aborts_even_after_valid_bytes():
    s3=adapter()
    with pytest.raises(Error) as failure:
        with s3.control.output([sys.executable,"-c","import sys;sys.stdout.buffer.write(b'x'*100);sys.stdout.flush();sys.exit(3)"]) as chunks:
            s3.upload_stream("key",chunks,32)
    assert failure.value.code=="TOOL_FAILED"
    assert "upload_part" in s3.calls
    assert "complete_multipart_upload" not in s3.calls
    assert s3.calls[-1]=="abort"


def test_stream_deadline_aborts():
    s3=adapter();s3.control=Control(.2)
    with pytest.raises(Error) as failure:
        with s3.control.output([sys.executable,"-c","import time;time.sleep(20)"]) as chunks:
            s3.upload_stream("key",chunks,32)
    assert failure.value.code=="DEADLINE_EXCEEDED"
    assert s3.calls[-1]=="abort"


def test_upload_failure_aborts():
    s3=adapter();original=s3.call
    def fail(operation,**kw):
        if operation=="upload_part":raise Error("S3_ACCESS_DENIED","Denied",8)
        return original(operation,**kw)
    s3.call=fail
    with pytest.raises(Error):s3.upload_stream("key",iter([b"data"]),2)
    assert s3.calls[-1]=="abort"


def test_metadata_inspector_can_finish_early():
    closed=[]
    def chunks():
        try:
            for _ in range(1000):yield b"x"*65536
        finally:closed.append(True)
    result=Control(5).inspect_input([sys.executable,"-c","import sys;sys.stdin.buffer.read(1);print('toc')"],chunks())
    assert result.strip()=="toc" and closed


@pytest.mark.parametrize("name,kind",[("../escape","file"),("toc.dat","symlink"),("nested/toc.dat","file")])
def test_directory_archive_rejects_unsafe(tmp_path,name,kind):
    path=tmp_path/"archive.tar"
    with tarfile.open(path,"w") as bundle:
        member=tarfile.TarInfo(name)
        if kind=="symlink":member.type=tarfile.SYMTYPE;member.linkname="/tmp/outside"
        else:member.size=1
        bundle.addfile(member,io.BytesIO(b"x") if kind=="file" else None)
    with pytest.raises(Error) as failure:
        with logical_archive({"format":"postgres-directory-tar","filename":path.name},tmp_path,tmp_path,Control()):pass
    assert failure.value.code=="UNSAFE_ARCHIVE"
    assert not list(tmp_path.glob("directory-restore-*"))
