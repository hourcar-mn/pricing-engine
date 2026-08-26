"""Pull the dashboard's CSV inputs from R2 when they are not on local disk.

Container disk is ephemeral -- it resets whenever the container stops -- so the
four input files live in R2 rather than in the image. They are too big for git
(~410MB) and contain per-user trip and invoice data, so they are not in the
repository either.

Every function here no-ops when the R2_* env vars are unset, so local runs read
``data/`` exactly as they always have and never need boto3 installed.

Upload the local copies to R2 (the one-time seed, and after each DataPrep
refresh) with:

    python r2_sync.py push
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
INPUT_PREFIX = "data/"

INPUT_FILES = [
    "clean_trips_with_invoice_plan_and_segment.csv",
    "user_month_regular.csv",
    "user_month_student.csv",
    "user_month_access.csv",
]

# The dashboard is threaded, so several API calls can discover the same missing
# file at once. Without this they would each start their own 258MB download and
# write over each other.
_DOWNLOAD_LOCK = threading.Lock()


def is_configured() -> bool:
    """True when R2 is wired up, i.e. we are running in the container."""
    return bool(os.getenv("R2_ENDPOINT") and os.getenv("R2_BUCKET"))


def _client():
    """boto3 S3 client pointed at R2, or (None, None) when R2 isn't configured."""
    if not is_configured():
        return None, None

    import boto3  # imported lazily so local runs don't need it installed

    client = boto3.client(
        "s3",
        endpoint_url=os.getenv("R2_ENDPOINT"),
        aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )
    return client, os.getenv("R2_BUCKET")


def _download(client, bucket: str, name: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    obj = client.get_object(Bucket=bucket, Key=INPUT_PREFIX + name)
    # Streamed rather than read() into memory -- the trip file is 258MB -- and
    # written to a temp name first so a download that dies partway through
    # leaves no half file that would later look present and parse as garbage.
    tmp = dest.with_suffix(dest.suffix + ".part")
    with open(tmp, "wb") as handle:
        shutil.copyfileobj(obj["Body"], handle)
    tmp.replace(dest)


def ensure_inputs(paths) -> None:
    """Fetch any of ``paths`` missing from disk. No-op when R2 isn't configured.

    Called from demand_engine.read_inputs on the first request rather than at
    container start, so the container passes its health check immediately and
    only the first API call pays for the download.
    """
    client, bucket = _client()
    if client is None:
        return

    with _DOWNLOAD_LOCK:
        for path in paths:
            path = Path(path)
            if path.exists():  # another thread got here first
                continue
            print(f"[r2] pulling {path.name}...", flush=True)
            _download(client, bucket, path.name, path)
            print(f"[r2] pulled {path.name}", flush=True)


def push() -> None:
    """Upload the local data/ files to R2. Run after a DataPrep refresh."""
    client, bucket = _client()
    if client is None:
        raise SystemExit(
            "R2 is not configured -- set R2_ENDPOINT, R2_BUCKET, "
            "R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY (see .env.example)"
        )

    for name in INPUT_FILES:
        path = DATA_DIR / name
        if not path.is_file():
            raise SystemExit(f"missing {path} -- run the DataPrep scripts first")
        print(f"[r2] uploading {name} ({path.stat().st_size / 1e6:.0f}MB)...")
        # upload_file, not put_object -- it multiparts the 258MB trip file
        client.upload_file(str(path), bucket, INPUT_PREFIX + name)
        print(f"[r2] uploaded {name}")


def _self_check() -> None:
    """Unconfigured is the local case and must stay a silent no-op."""
    saved = {k: os.environ.pop(k, None) for k in ("R2_ENDPOINT", "R2_BUCKET")}
    try:
        assert not is_configured()
        assert _client() == (None, None)
        # the point of the no-op: a missing file stays missing, nothing raises,
        # and boto3 is never imported
        ghost = DATA_DIR / "definitely_not_here.csv"
        ensure_inputs([ghost])
        assert not ghost.exists()

        os.environ["R2_ENDPOINT"] = "https://example.invalid"
        os.environ["R2_BUCKET"] = "bucket"
        assert is_configured()
    finally:
        for key, value in saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
    print("r2_sync self-check ok")


if __name__ == "__main__":
    import sys

    try:  # the container gets real env vars; locally .env is the convenient way
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command == "push":
        push()
    elif command == "check":
        _self_check()
    else:
        raise SystemExit(f"usage: python r2_sync.py [push|check]")
