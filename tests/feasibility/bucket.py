"""Test harness ONLY: create a synthetic bucket in the local emulator."""
import time
import boto3

s3 = boto3.client("s3", endpoint_url="http://127.0.0.1:19000", region_name="us-east-1",
                  aws_access_key_id="fixture-access", aws_secret_access_key="fixture-secret-only")
for attempt in range(30):
    try:
        s3.create_bucket(Bucket="pgbk-test")
        print("Local synthetic fixture bucket ready")
        break
    except Exception:
        if attempt == 29:
            raise
        time.sleep(1)
