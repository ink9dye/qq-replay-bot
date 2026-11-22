#!/usr/bin/env python3
"""Bounded background repair for recently missing and active Super Pre images."""
import argparse
import base64
import io
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

LIMIT = 2
LOG_TAIL = 128 * 1024
RECENT_SECONDS = 48 * 3600
RETRY_SECONDS = 15 * 60
MAX_STATE_BYTES = 512 * 1024
WORKER_ENTRY = "image-maintenance"
VALID_SUFFIXES = {".webp", ".jpg", ".png"}
SYSTEM_PRESSURE = 35.0
MIN_AVAILABLE_KIB = 200 * 1024


def read_json(path, maximum=MAX_STATE_BYTES):
    try:
        with path.open("rb") as source:
            blob = source.read(maximum + 1)
        if len(blob) <= maximum:
            value = json.loads(blob)
            if isinstance(value, dict):
                return value
    except (OSError, ValueError):
        pass
    return {}


def write_atomic(path, blob):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(blob)
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def write_state(path, state):
    write_atomic(path, json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def available_for_background():
    try:
        memory = Path("/proc/meminfo").read_text()
        available = int(re.search(r"^MemAvailable:\s+(\d+)", memory, re.M)[1])
        for resource in ("io", "memory"):
            pressure = Path("/proc/pressure", resource).read_text()
            average = float(re.search(r"^full avg10=([0-9.]+)", pressure, re.M)[1])
            if average >= SYSTEM_PRESSURE:
                return False
        return available >= MIN_AVAILABLE_KIB
    except (OSError, ValueError, TypeError):
        return False


def active_catalog(snapshot):
    catalog = {}
    for key, item in snapshot.items():
        if not (isinstance(key, str) and key.isascii() and key.isdecimal() and isinstance(item, dict)):
            continue
        code = int(key)
        source = item.get("picUrl")
        name = item.get("name")
        if (not 100_000_000 <= code <= 0xFFFFFFFF or item.get("id") != code or
                not isinstance(source, str) or not isinstance(name, str)):
            continue
        if re.fullmatch(rf"https://cdntx\.moecube\.com/ygopro-super-pre/data/pics/{code}\.jpg"
                        r"(?:\?version=[\w.-]+)?", source, flags=re.ASCII):
            catalog[code] = (name, source)
    return catalog


def recent_misses(path, now):
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            offset = max(0, source.tell() - LOG_TAIL)
            source.seek(offset)
            if offset:
                source.readline(LOG_TAIL)
            lines = source.read(LOG_TAIL).splitlines()
    except OSError:
        return {}
    missing = {}
    for line in lines:
        try:
            row = json.loads(line)
            code, want, stamped = row["code"], row["want"], float(row["t"])
        except (ValueError, KeyError, TypeError):
            continue
        if (type(code) is not int or not 0 < code <= 0xFFFFFFFF or want not in ("thumb", "full") or
                row.get("entry") in (WORKER_ENTRY, "miss-audit") or not 0 <= now - stamped <= RECENT_SECONDS):
            continue
        key = code, want
        missing[key] = max(missing.get(key, 0), stamped)
    return missing


def existing_thumbnails(data):
    try:
        return {int(path.stem) for path in (data / "card-thumbs").iterdir()
                if path.suffix.lower() in VALID_SUFFIXES and path.stem.isascii() and path.stem.isdecimal()}
    except OSError:
        return set()


def pick_jobs(data, cache, catalog, state, now):
    old = state.get("catalog")
    previous_pending = state.get("pending") or []
    pending = {code for code in catalog if str(code) in previous_pending}
    conflicts = set(state.get("identity_conflicts") or [])
    if catalog and isinstance(old, dict):
        for code, release in catalog.items():
            previous = old.get(str(code))
            if previous != list(release):
                pending.add(code)
                if previous and previous[0] != release[0]:
                    conflicts.add(str(code))
    thumbnail_codes = existing_thumbnails(data)
    missing = recent_misses(data / "logs" / "card-image-misses.jsonl", now)
    requested = dict.fromkeys((code, "superpre") for code in reversed(sorted(pending)))
    for code, want in sorted(missing, key=missing.get, reverse=True):
        if code in catalog:
            requested[(code, "superpre")] = None
        else:
            requested[(code, want)] = None
    requested.update(((code, "superpre"), None) for code in reversed(sorted(catalog.keys() - thumbnail_codes)))
    # Do not use an inferred source for a Super Pre number absent from the current release.
    retry = {key: deadline for key, deadline in state.get("retry", {}).items()
             if type(deadline) in (int, float) and now < deadline <= now + RETRY_SECONDS}
    jobs = []
    for code, want in requested:
        if len(jobs) >= LIMIT:
            break
        if f"{code}:{want}" in retry:
            continue
        if want == "superpre":
            source = catalog[code][1]
            conflict = str(code) in conflicts
            if conflict and "?version=" not in source:
                continue
            if code not in pending and (conflict or cache.local_thumb(data, code)):
                preview = read_json(data / "cache" / "qq-superpre-previews" / f"{code}.json")
                if "?version=" not in source or (preview.get("source") == source and preview.get("image")):
                    continue
        elif code >= 100_000_000 or (cache.local_thumb if want == "thumb" else cache.local_full)(data, code):
            continue
        jobs.append((code, want))
    next_state = {
        "catalog": {str(code): list(item) for code, item in catalog.items()} if catalog else old,
        "pending": sorted(map(str, pending)) if catalog else previous_pending,
        "identity_conflicts": sorted(conflicts),
        "retry": retry,
    }
    return jobs, next_state


def repair_superpre(data, cache, preview_bytes, code, source, catalog_path, catalog_stamp, *, preview_only=False):
    """Prepare all formats before publishing a verified image for the current release."""
    from PIL import Image
    blob = cache._download(source, timeout=6)
    if not cache._valid_image(blob):
        return False
    with Image.open(io.BytesIO(blob)) as image:
        if image.format != "JPEG" or max(image.size) > 4096:
            return False
        image.verify()
    with Image.open(io.BytesIO(blob)) as image:
        converted = image.convert("RGB")
        converted.thumbnail(cache.THUMB_MAX, Image.Resampling.LANCZOS)
        output = io.BytesIO()
        converted.save(output, "WEBP", quality=82, method=4)
    if catalog_path.stat().st_mtime_ns != catalog_stamp or not available_for_background():
        return False
    payload = None
    if "?version=" in source:
        payload = json.dumps({"source": source, "image": base64.b64encode(preview_bytes(blob)).decode("ascii")})
    if not preview_only:
        write_atomic(cache.full_path(data, code), blob)
        write_atomic(cache.thumb_path(data, code), output.getvalue())
    if payload is not None:
        write_atomic(data / "cache" / "qq-superpre-previews" / f"{code}.json", payload.encode("utf-8"))
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("/opt/ygobot/data"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check-pressure", action="store_true")
    args = parser.parse_args()
    if args.check_pressure:
        return 0 if available_for_background() else 1
    data = args.data_dir.resolve()
    if not available_for_background():
        print("card-image maintenance deferred: host pressure")
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bot import card_image_cache as cache
    from bot.card_search import _qq_preview_bytes

    now = time.time()
    catalog_path = data / "cache" / "superpre-cards.json"
    try:
        stamp = catalog_path.stat().st_mtime_ns
    except OSError:
        stamp = 0
    catalog = active_catalog(read_json(catalog_path))
    state_path = data / "cache" / "card-image-maintenance.json"
    old_state = read_json(state_path)
    jobs, state = pick_jobs(data, cache, catalog, old_state, now)
    if args.dry_run:
        print(json.dumps(dict(planned=len(jobs), superpre=sum(w == "superpre" for _, w in jobs))))
        return 0
    repaired = 0
    for code, want in jobs:
        if not available_for_background():
            print("card-image maintenance deferred during run: host pressure")
            break
        key = f"{code}:{want}"
        state["retry"][key] = time.time() + RETRY_SECONDS
        write_state(state_path, state)
        try:
            if want == "superpre":
                conflict = str(code) in state["identity_conflicts"]
                if conflict:
                    print(f"card-image maintenance code={code} identity_conflict=1; preserve shared image", flush=True)
                fixed = repair_superpre(data, cache, _qq_preview_bytes, code, catalog[code][1],
                                        catalog_path, stamp, preview_only=conflict)
            elif want == "thumb":
                fixed = bool(cache.ensure_thumb(data, code, timeout=4))
            else:
                fixed = bool(cache.ensure_full(data, code, timeout=4))
        except (OSError, ValueError):
            fixed = False
        if fixed:
            repaired += 1
            state["retry"].pop(key, None)
            if str(code) in state["pending"]:
                state["pending"].remove(str(code))
        print(f"card-image maintenance code={code} part={want} fixed={int(fixed)}", flush=True)
    if state != old_state:
        write_state(state_path, state)
    print(f"card-image maintenance planned={len(jobs)} repaired={repaired} pending_releases={len(state['pending'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

