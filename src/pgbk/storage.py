import hashlib
import os
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .credentials import secret, secret_path
from .errors import Error, require


def hash_file(path):
    h, size = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            size += len(chunk)
            h.update(chunk)
    return h.hexdigest(), size


class S3:
    def __init__(self, config, control):
        self.config, self.control = config, control
        ref = config["credentialsSecretRef"]
        self.access = secret(ref, "accessKeyId")
        self.password = secret(ref, "secretAccessKey")
        self.token = secret(ref, "sessionToken", optional=True)
        verify = secret_path(config["tls"]["caSecretRef"], "ca.crt") if config["tls"].get("caSecretRef") else True
        self.client = boto3.client("s3", endpoint_url=config["endpoint"], region_name=config["region"],
                                   aws_access_key_id=self.access, aws_secret_access_key=self.password,
                                   aws_session_token=self.token, verify=verify,
                                   config=Config(s3={"addressing_style": config["addressingStyle"]},
                                                 connect_timeout=10, read_timeout=15, retries={"max_attempts": 0},
                                                 request_checksum_calculation="when_required",
                                                 response_checksum_validation="when_required"))
        self.bucket = config["bucket"]

    def call(self, operation, _attempts=5, **kwargs):
        for attempt in range(_attempts):
            self.control.check()
            try:
                return getattr(self.client, operation)(Bucket=self.bucket, **kwargs)
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code in ["NoSuchKey", "404", "NotFound"]:
                    return None
                if code in ["AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken"]:
                    raise Error("S3_ACCESS_DENIED", "S3 authorization failed", 8) from None
                if code not in ["SlowDown", "RequestTimeout", "InternalError", "ServiceUnavailable", "500", "503"]:
                    raise Error("S3_OPERATION_FAILED", "S3 operation failed", 5, operation=operation) from None
            except BotoCoreError:
                pass
            if attempt == _attempts - 1:
                raise Error("S3_RETRY_EXHAUSTED", "S3 retry budget exhausted", 5, operation=operation)
            self.control.stop.wait(min(2 ** attempt, 8))

    def read_object(self, key, limit=None):
        for attempt in range(5):
            try:
                result = self.call("get_object", _attempts=1, Key=key)
                if result is None:
                    return None
                h, size, chunks = hashlib.sha256(), 0, []
                with result["Body"] as stream:
                    while True:
                        self.control.check()
                        chunk = stream.read(1024 * 1024 if limit is None else min(1024 * 1024, limit + 1 - size))
                        if not chunk:
                            break
                        size += len(chunk)
                        if limit is None:
                            h.update(chunk)
                        else:
                            require(size <= limit, "CATALOG_SIZE_LIMIT", "Catalog metadata exceeds size limit", 6)
                            chunks.append(chunk)
                self.control.check()
                return (h.hexdigest(), size) if limit is None else b"".join(chunks)
            except Error as error:
                if error.code != "S3_RETRY_EXHAUSTED":
                    raise
            except BotoCoreError:
                pass  # A truncated stream is retried from byte zero, with a fresh hash.
            if attempt == 4:
                raise Error("S3_RETRY_EXHAUSTED", "S3 object read retry budget exhausted", 5)
            self.control.stop.wait(min(2 ** attempt, 8))

    def get(self, key, limit=8 * 1024 * 1024):
        return self.read_object(key, limit)

    def put(self, key, data):
        self.call("put_object", Key=key, Body=data, ContentType="application/json")

    def delete(self, key):
        self.call("delete_object", Key=key)

    def keys(self, prefix):
        token = None
        while True:
            args = {"Prefix": prefix}
            if token:
                args["ContinuationToken"] = token
            page = self.call("list_objects_v2", **args)
            for value in page.get("Contents", []):
                yield value["Key"]
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]

    def remote_hash(self, key):
        result = self.read_object(key)
        require(result is not None, "ARTIFACT_MISSING", "Stored artifact is missing", 6)
        return result

    def object_chunks(self, key):
        result = self.call("get_object", Key=key)
        require(result is not None, "ARTIFACT_MISSING", "Stored artifact is missing", 6)
        with result["Body"] as body:
            while True:
                self.control.check()
                chunk = body.read(1024 * 1024)
                if not chunk:
                    return
                yield chunk

    def upload_stream(self, key, chunks, part_size):
        """Bounded, sequential multipart upload. Producer must succeed before commit."""
        upload = self.call("create_multipart_upload", Key=key)["UploadId"]
        parts, buffer = [], bytearray()
        digest, size = hashlib.sha256(), 0
        def part(data):
            require(len(parts) < 10000, "MULTIPART_LIMIT", "Increase partSizeMiB for this backup size", 3)
            result = self.call("upload_part", Key=key, UploadId=upload,
                               PartNumber=len(parts) + 1, Body=data)
            parts.append({"PartNumber": len(parts) + 1, "ETag": result["ETag"]})
        try:
            for chunk in chunks:
                self.control.check()
                digest.update(chunk)
                size += len(chunk)
                buffer.extend(chunk)
                while len(buffer) >= part_size:
                    part(bytes(buffer[:part_size]))
                    del buffer[:part_size]
            self.control.check()
            require(size > 0, "EMPTY_ARTIFACT", "Streaming producer returned no archive", 6)
            if buffer:
                part(bytes(buffer))
            try:
                self.call("complete_multipart_upload", Key=key, UploadId=upload, MultipartUpload={"Parts": parts})
            except Error as error:
                # A successful complete response can be lost. Reconcile exact bytes only.
                if error.code not in ["S3_RETRY_EXHAUSTED", "S3_OPERATION_FAILED"] or self.remote_hash(key) != (digest.hexdigest(), size):
                    raise
            return digest.hexdigest(), size
        except BaseException:
            try:
                # Cleanup still attempted after cancellation/deadline; client timeouts bound it.
                self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload)
            except Exception:
                pass  # Bucket lifecycle must clean abandoned uploads after node/process loss.
            raise

    def wal_env(self, scratch):
        env = {"PATH": os.environ["PATH"], "HOME": str(scratch), "TMPDIR": str(scratch),
               "WALG_S3_PREFIX": "s3://" + self.bucket,
               "AWS_ENDPOINT": self.config["endpoint"], "AWS_REGION": self.config["region"],
               "AWS_S3_FORCE_PATH_STYLE": str(self.config["addressingStyle"] == "path").lower(),
               "AWS_ACCESS_KEY_ID": self.access, "AWS_SECRET_ACCESS_KEY": self.password,
               "WALG_LOG_LEVEL": "ERROR"}
        if self.token:
            env["AWS_SESSION_TOKEN"] = self.token
        if self.config["tls"].get("caSecretRef"):
            env["SSL_CERT_FILE"] = secret_path(self.config["tls"]["caSecretRef"], "ca.crt")
        return env

    def transfer(self, direction, key, path):
        args = ["wal-g", "st", "put", str(path), key, "--no-compress", "--no-encrypt"] if direction == "put" else [
            "wal-g", "st", "get", key, str(path), "--no-decompress", "--no-decrypt"]
        # A lost put response is safely reconciled by the caller's remote hash check.
        for attempt in range(5):
            try:
                self.control.run(args, env=self.wal_env(Path(path).parent))
                return
            except Error as original:
                if original.code != "TOOL_FAILED":
                    raise
                if direction == "put":
                    try:
                        if self.remote_hash(key) == hash_file(path):
                            return
                    except Error as remote_error:
                        if remote_error.exit_code == 8:
                            raise
                if attempt == 4:
                    raise
                if direction == "get" and Path(path).exists():
                    Path(path).unlink()
                self.control.stop.wait(min(2 ** attempt, 8))
