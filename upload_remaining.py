#!/usr/bin/env python3
"""Upload remaining files to Frame.io that were missed during the initial transfer."""

import argparse
import json
import logging
import math
import mimetypes
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import requests
from dotenv import load_dotenv
from frameioclient import FrameioClient
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).parent / "data"
LOGS_DIR = Path(__file__).parent / "logs"
FOLDER_IDS_CSV = DATA_DIR / "folder_ids.csv"
MISSING_FILES_TXT = DATA_DIR / "missing_files.txt"
PROGRESS_FILE = DATA_DIR / "upload_progress.json"

MAX_RETRIES = 5
BACKOFF_BASE = 3  # seconds


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging():
    """Configure logging to both console and a timestamped log file.

    Log files are never deleted or rotated away — each run gets its own file.
    """
    LOGS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOGS_DIR / f"upload_{ts}.log"

    # File handler — everything goes here
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    # Console handler — INFO and above (tqdm handles progress)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(levelname)-7s | %(message)s"))

    logger = logging.getLogger("uploader")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    logger.info(f"Log file: {log_file}")
    return logger


# ---------------------------------------------------------------------------
# Asset ID ledger — append-only, never deleted
# ---------------------------------------------------------------------------

ASSET_LEDGER = DATA_DIR / "asset_ledger.jsonl"

def record_asset(filepath, asset_id, folder_id, status, attempt=None, error=None):
    """Append a line to the asset ledger. Every create_asset call gets recorded."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "filepath": filepath,
        "asset_id": asset_id,
        "folder_id": folder_id,
        "status": status,  # "created", "uploaded", "upload_failed"
    }
    if attempt is not None:
        entry["attempt"] = attempt
    if error is not None:
        entry["error"] = str(error)[:500]
    with _ledger_lock:
        with open(ASSET_LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

_ledger_lock = Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log = logging.getLogger("uploader")


def load_folder_map():
    """Load folder_ids.csv into {normalized_path: frameio_id}."""
    folder_map = {}
    with open(FOLDER_IDS_CSV, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            uuid, local_path = line.split("|", 1)
            normalized = local_path.replace("\\", "/")
            folder_map[normalized] = uuid
    return folder_map


def load_missing_files(folder_filter=None):
    """Load missing_files.txt, optionally filtering by folder substring."""
    files = []
    with open(MISSING_FILES_TXT, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if folder_filter and folder_filter not in line:
                continue
            files.append(line)
    return files


def load_progress():
    """Load dict of already-uploaded file paths -> asset IDs."""
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Migrate from old format (list of paths) to new format (dict path->asset_id)
            if isinstance(data, list):
                return {path: None for path in data}
            return data
    return {}


def save_progress(completed):
    """Persist the dict of completed uploads."""
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(completed, f)


def resolve_parent_folder(filepath, folder_map, client, folder_cache, cache_lock):
    """
    Walk up from the file's parent dir until we find a known folder ID.
    Create any missing intermediate folders on Frame.io and cache them.
    """
    parent_dir = os.path.dirname(filepath)  # forward-slash path

    # Check cache/map directly
    if parent_dir in folder_cache:
        return folder_cache[parent_dir]
    if parent_dir in folder_map:
        folder_cache[parent_dir] = folder_map[parent_dir]
        return folder_map[parent_dir]

    # Walk up to find nearest known ancestor
    parts_to_create = []
    current = parent_dir
    while current and current not in folder_map and current not in folder_cache:
        parts_to_create.append(os.path.basename(current))
        current = os.path.dirname(current)

    if not current or (current not in folder_map and current not in folder_cache):
        return None

    ancestor_id = folder_cache.get(current) or folder_map.get(current)

    # Create folders top-down (must be serialized per-path to avoid dupes)
    with cache_lock:
        # Re-check after acquiring lock — another thread may have created it
        if parent_dir in folder_cache:
            return folder_cache[parent_dir]

        current_id = ancestor_id
        current_path = current
        for folder_name in reversed(parts_to_create):
            current_path = current_path + "/" + folder_name
            # Check again in case partially created
            if current_path in folder_cache:
                current_id = folder_cache[current_path]
                continue
            folder_asset = client.create_asset(
                current_id, name=folder_name, type="folder"
            )
            current_id = folder_asset["id"]
            folder_cache[current_path] = current_id
            log.info(f"Created folder: {folder_name} -> {current_id} (parent: {ancestor_id})")

        return current_id


def upload_single_file(filepath, folder_id, client, attempt):
    """Create asset on Frame.io and upload the file data. Returns asset ID."""
    filesize = os.path.getsize(filepath)
    filetype = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
    filename = os.path.basename(filepath)

    # Create the asset record
    asset = client.create_asset(
        folder_id,
        name=filename,
        type="file",
        filetype=filetype,
        filesize=filesize,
    )
    asset_id = asset["id"]

    # Record immediately — if upload fails, we still have the asset ID
    record_asset(filepath, asset_id, folder_id, "created", attempt=attempt)
    log.debug(f"Created asset: {asset_id} for {filename} (attempt {attempt})")

    upload_urls = asset["upload_urls"]
    chunk_size = int(math.ceil(filesize / len(upload_urls))) if upload_urls else filesize

    try:
        with open(filepath, "rb") as f:
            for i, url in enumerate(upload_urls):
                chunk = f.read(chunk_size)
                resp = requests.put(
                    url,
                    data=chunk,
                    headers={"content-type": filetype, "x-amz-acl": "private"},
                    timeout=300,  # 5 min per chunk
                )
                resp.raise_for_status()
    except Exception as e:
        record_asset(filepath, asset_id, folder_id, "upload_failed", attempt=attempt, error=e)
        raise

    record_asset(filepath, asset_id, folder_id, "uploaded", attempt=attempt)
    return asset_id


def upload_with_retry(filepath, folder_id, client):
    """Upload with exponential backoff retry. Returns asset ID."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return upload_single_file(filepath, folder_id, client, attempt)
        except requests.exceptions.HTTPError as e:
            last_error = e
            log.warning(f"Attempt {attempt}/{MAX_RETRIES} failed for {os.path.basename(filepath)}: {e}")
            if e.response is not None and e.response.status_code == 429:
                wait = BACKOFF_BASE ** attempt * 5  # longer wait for rate limits
                time.sleep(wait)
            elif attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE ** attempt)
            else:
                raise
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = e
            log.warning(f"Attempt {attempt}/{MAX_RETRIES} failed for {os.path.basename(filepath)}: {type(e).__name__}")
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE ** attempt)
            else:
                raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Upload remaining files to Frame.io")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be uploaded")
    parser.add_argument("--folder", type=str, default=None, help="Filter to folder substring")
    parser.add_argument("--workers", type=int, default=4, help="Number of upload workers")
    parser.add_argument("--reset", action="store_true", help="Clear progress and start fresh")
    args = parser.parse_args()

    logger = setup_logging()

    load_dotenv()
    token = os.getenv("FRAMEIO_TOKEN")
    if not token:
        logger.error("FRAMEIO_TOKEN not set in environment or .env file")
        sys.exit(1)

    client = FrameioClient(token)

    # Verify auth
    me = client.get_me()
    logger.info(f"Authenticated as: {me.get('name')} ({me.get('email')})")

    # Load data
    folder_map = load_folder_map()
    logger.info(f"Loaded {len(folder_map)} folder mappings")

    missing_files = load_missing_files(args.folder)
    logger.info(f"Found {len(missing_files)} files to process")

    if args.reset and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        logger.info("Progress reset.")

    completed = load_progress()
    remaining = [f for f in missing_files if f not in completed]
    logger.info(f"Already uploaded: {len(completed)} | Remaining: {len(remaining)}")

    if not remaining:
        logger.info("Nothing to upload!")
        return

    if args.dry_run:
        # Resolve folders and show plan
        folder_cache = {}
        cache_lock = Lock()
        for fp in remaining[:50]:  # show first 50
            parent_dir = os.path.dirname(fp)
            fid = folder_map.get(parent_dir) or folder_cache.get(parent_dir)
            status = fid or "NEEDS CREATION"
            print(f"  {os.path.basename(fp)} -> {status}")
        if len(remaining) > 50:
            print(f"  ... and {len(remaining) - 50} more")
        print(f"\nDry run complete. {len(remaining)} files would be uploaded.")
        return

    # Upload
    folder_cache = {}
    cache_lock = Lock()
    progress_lock = Lock()
    success_count = 0
    fail_count = 0

    pbar = tqdm(total=len(remaining), unit="file", desc="Uploading")

    def process_file(filepath):
        nonlocal success_count, fail_count
        # Convert forward-slash path to OS path for reading
        local_path = filepath.replace("/", os.sep)

        if not os.path.exists(local_path):
            log.error(f"File not found: {filepath}")
            with progress_lock:
                fail_count += 1
                pbar.update(1)
            return

        try:
            folder_id = resolve_parent_folder(
                filepath, folder_map, client, folder_cache, cache_lock
            )
            if not folder_id:
                log.error(f"Could not resolve parent folder: {filepath}")
                with progress_lock:
                    fail_count += 1
                    pbar.update(1)
                return

            asset_id = upload_with_retry(local_path, folder_id, client)

            with progress_lock:
                completed[filepath] = asset_id
                success_count += 1
                pbar.update(1)
                # Save progress every 50 files
                if success_count % 50 == 0:
                    save_progress(completed)

        except Exception as e:
            log.error(f"FAILED: {filepath} | {e}")
            with progress_lock:
                fail_count += 1
                pbar.update(1)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_file, fp): fp for fp in remaining}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass  # already handled inside process_file

    pbar.close()
    save_progress(completed)

    logger.info("=" * 60)
    logger.info("Upload complete!")
    logger.info(f"  Successful: {success_count}")
    logger.info(f"  Failed:     {fail_count}")
    logger.info(f"  Total:      {success_count + fail_count}")
    if fail_count > 0:
        logger.info(f"  Check logs/ directory and {ASSET_LEDGER} for details")


if __name__ == "__main__":
    main()
