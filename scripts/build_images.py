"""Build matching-major development workers from pinned base images."""
import argparse
import json
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--major", type=int, choices=range(14, 19), action="append")
parser.add_argument("--repository", default="pgbk-worker")
parser.add_argument("--engine", choices=("docker", "podman"), default="docker")
args = parser.parse_args()
matrix = json.load(open("docs/compatibility.json"))
for major in args.major or range(14, 19):
    subprocess.run([args.engine, "build", "--platform", "linux/amd64", "--build-arg",
                    "POSTGRES_BASE=" + matrix["postgres"][str(major)]["base"],
                    "-t", f"{args.repository}:{major}-test", "-f", "images/Dockerfile", "."], check=True)
