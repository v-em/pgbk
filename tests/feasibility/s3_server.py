"""Moto S3 fixture with bounded WSGI writes (macOS sendall rejects >2-GiB writes)."""
import argparse
from moto.server import create_backend_app
from werkzeug.serving import run_simple

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port',type=int,default=19001)
args=parser.parse_args()
app=create_backend_app('s3')

def chunked(environ,start_response):
    result=app(environ,start_response)
    try:
        for data in result:
            for start in range(0,len(data),1024*1024):
                yield data[start:start+1024*1024]
    finally:
        if hasattr(result,'close'): result.close()

run_simple('0.0.0.0',args.port,chunked,threaded=True)
