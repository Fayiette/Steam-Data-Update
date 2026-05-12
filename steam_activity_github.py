"""
Steam Activity Fetcher for GitHub Actions
Downloads all Steam artifact files from R2 at startup, runs the existing
playtime/genre processing against script-relative paths, then uploads
every artifact file back to R2 using flat object names.
"""

import os
import json
import requests
import csv
import time
import hashlib
import boto3
import sys
import traceback
from botocore.exceptions import ClientError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv


load_dotenv()


DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")

API_KEY = os.environ["STEAM_API_KEY"]
STEAM_ID = os.environ["STEAM_ID64"]

# Script-relative artifact directory (Actions-friendly).
SCRIPT_DIR = Path(__file__).resolve().parent

ACTIVITY_CSV_NAME = os.getenv("ACTIVITY_CSV_NAME", "").strip()
GENRE_CACHE_NAME = os.getenv("GENRE_CACHE_NAME", "").strip()
if not ACTIVITY_CSV_NAME:
    print("ACTIVITY_CSV_NAME must be set in the environment.", file=sys.stderr)
    sys.exit(1)
if not GENRE_CACHE_NAME:
    print("GENRE_CACHE_NAME must be set in the environment.", file=sys.stderr)
    sys.exit(1)

ACTIVITY_CSV_PATH = SCRIPT_DIR / ACTIVITY_CSV_NAME
GENRE_CACHE_PATH = SCRIPT_DIR / GENRE_CACHE_NAME

# Every file synced to/from R2 (flat keys = local filenames).
ARTIFACT_FILES = [
    ACTIVITY_CSV_PATH,
    GENRE_CACHE_PATH,
]

R2_BUCKET = os.getenv("R2_BUCKET")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_ENDPOINT = os.getenv("R2_ENDPOINT")


def s3_client():
    session = boto3.session.Session()
    return session.client(
        "s3",
        region_name="auto",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    )


def compute_file_hash(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


DISCORD_MESSAGE_MAX_LEN = 2000


def format_failure_for_discord(mention: str, run_ts: int, exc: BaseException) -> str:
    """Build a Discord-safe failure body (private channel); capped at Discord content limit."""
    header = f"{mention}Steam Activity (GitHub) failed at <t:{run_ts}:f>\n"
    exc_line = f"{exc!s}"
    tb = traceback.format_exc()
    body = f"{header}{exc_line}\n\n{tb}"
    if len(body) <= DISCORD_MESSAGE_MAX_LEN:
        return body
    prefix = f"{header}{exc_line}\n\n"
    room = DISCORD_MESSAGE_MAX_LEN - len(prefix) - 1
    if room < 1:
        return body[:DISCORD_MESSAGE_MAX_LEN]
    return prefix + tb[:room] + "…"


def send_discord_alert(message: str):
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
        print("Discord alert sent.")
    except Exception:
        print("Failed to send Discord alert.")


def download_artifacts_from_r2(s3) -> None:
    """Pull each artifact file from R2 into the script directory if it exists."""
    print("\n=== Pulling artifacts from R2 ===")
    for path in ARTIFACT_FILES:
        key = path.name
        try:
            s3.download_file(R2_BUCKET, key, str(path))
            print(f"Downloaded {key} from R2")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey"}:
                print(f"No existing {key} in R2 - starting fresh")
            else:
                print(f"Could not download {key} from R2: {e}")
        except Exception as e:
            print(f"Could not download {key} from R2: {e}")


def upload_artifacts_to_r2(s3) -> dict:
    """Upload each artifact file back to R2 (flat key) when content changed."""
    print("\n=== Uploading artifacts to R2 ===")
    results = {}
    for path in ARTIFACT_FILES:
        key = path.name
        if not path.exists():
            print(f"Skipping {key} - not present locally")
            results[key] = "missing"
            continue

        local_hash = compute_file_hash(path)
        try:
            obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
            remote_hash = hashlib.sha256(obj["Body"].read()).hexdigest()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey"}:
                remote_hash = None
            else:
                print(f"Failed remote hash check for {key}: {e}")
                results[key] = "error"
                continue

        if local_hash == remote_hash:
            print(f"No change for {key} - skipping upload")
            results[key] = "no-change"
            continue

        content_type = "text/csv" if path.suffix.lower() == ".csv" else "application/json"
        try:
            s3.upload_file(
                str(path),
                R2_BUCKET,
                key,
                ExtraArgs={"ACL": "public-read", "ContentType": content_type},
            )
            print(f"Uploaded {key} to R2")
            results[key] = "uploaded"
        except Exception as e:
            print(f"Failed to upload {key}: {e}")
            results[key] = "error"

    return results


def load_genre_cache() -> dict:
    if GENRE_CACHE_PATH.is_file():
        with open(GENRE_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_genre_cache(cache: dict) -> None:
    with open(GENRE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def fetch_recent_games() -> list:
    url = (
        "https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/"
        f"?key={API_KEY}&steamid={STEAM_ID}"
    )
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()
    return data.get("response", {}).get("games", [])


def read_existing_rows() -> list:
    rows = []
    if ACTIVITY_CSV_PATH.exists():
        with open(ACTIVITY_CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    return rows


def build_total_deltas(existing_rows: list) -> dict:
    totals = {}
    for row in existing_rows:
        appid = row["appid"]
        delta = int(row["playtime_today"])
        totals[appid] = totals.get(appid, 0) + delta
    return totals


def enrich_with_genre(appid: str, genre_cache: dict):
    cached = genre_cache.get(appid, {})
    genres = cached.get("genres", "Unknown")
    is_paid = cached.get("is_paid", "Paid")
    categories = cached.get("categories", "Unknown")

    if cached:
        return genres, is_paid, categories

    try:
        store_url = (
            f"https://store.steampowered.com/api/appdetails?appids={appid}&l=en&cc=us"
        )
        store_resp = requests.get(store_url, timeout=15)
        store_data = store_resp.json()
        app_data = store_data.get(str(appid), {}).get("data", {})

        genre_list = app_data.get("genres", [])
        genres = (
            ";".join([g["description"] for g in genre_list]) if genre_list else "Unknown"
        )

        is_paid = "Paid" if not app_data.get("is_free", False) else "Free"

        category_list = app_data.get("categories", [])
        categories = (
            ";".join([c["description"] for c in category_list])
            if category_list
            else "Unknown"
        )

        genre_cache[appid] = {
            "genres": genres,
            "is_paid": is_paid,
            "categories": categories,
        }

        # Respect Steam store API rate limits.
        time.sleep(1)

    except Exception as e:
        print(f"Failed to fetch genre info for appid {appid}: {e}")
        genres = "Unknown"
        is_paid = "Paid"
        categories = "Unknown"

    return genres, is_paid, categories


def build_new_rows(games: list, genre_cache: dict, total_deltas: dict, day_iso: str, populate_iso: str) -> list:
    new_rows = []
    for game in games:
        appid = str(game.get("appid", ""))
        name = game.get("name", "")
        pt_forever = game.get("playtime_forever", 0)

        previous_total = total_deltas.get(appid, 0)
        delta = max(pt_forever - previous_total, 0)
        if delta == 0:
            continue

        genres, is_paid, categories = enrich_with_genre(appid, genre_cache)

        new_rows.append(
            {
                "timestamp": day_iso,
                "appid": appid,
                "name": name,
                "genres": genres,
                "is_paid": is_paid,
                "game_categories": categories,
                "playtime_today": delta,
                "populate_date": populate_iso,
            }
        )
    return new_rows


def group_and_sum(existing_rows: list, new_rows: list) -> list:
    grouped = {}
    for row in existing_rows + new_rows:
        key = (
            row["timestamp"],
            row["appid"],
            row["name"],
            row["genres"],
            row["is_paid"],
            row["game_categories"],
        )
        row["playtime_today"] = int(row["playtime_today"])
        if key in grouped:
            grouped[key]["playtime_today"] += row["playtime_today"]
        else:
            grouped[key] = row

    final_rows = []
    for value in grouped.values():
        final_rows.append(
            [
                value["timestamp"],
                value["appid"],
                value["name"],
                value["genres"],
                value["is_paid"],
                value["game_categories"],
                value["playtime_today"],
                value["populate_date"],
            ]
        )
    return final_rows


def write_activity_csv(final_rows: list) -> None:
    with open(ACTIVITY_CSV_PATH, "w", newline="", encoding="utf-8") as f_csv:
        writer = csv.writer(f_csv)
        writer.writerow(
            [
                "timestamp",
                "appid",
                "name",
                "genres",
                "is_paid",
                "game_categories",
                "playtime_today",
                "populate_date",
            ]
        )
        writer.writerows(final_rows)
    print(f"Local CSV saved to {ACTIVITY_CSV_PATH}")


def process_steam_activity() -> bool:
    """Returns True when there is something to write/upload, False when no activity."""
    real_timestamp = datetime.now(timezone.utc)
    # Treat early-morning runs as still belonging to the previous day.
    adjusted_timestamp = (
        real_timestamp - timedelta(days=1) if real_timestamp.hour < 7 else real_timestamp
    )
    day_iso = adjusted_timestamp.date().isoformat()
    populate_iso = real_timestamp.isoformat()

    games = fetch_recent_games()
    if not games:
        print("No recent playtime data. Nothing new to log.")
        return False

    existing_rows = read_existing_rows()
    total_deltas = build_total_deltas(existing_rows)

    genre_cache = load_genre_cache()
    new_rows = build_new_rows(games, genre_cache, total_deltas, day_iso, populate_iso)
    save_genre_cache(genre_cache)

    final_rows = group_and_sum(existing_rows, new_rows)
    write_activity_csv(final_rows)
    return True


def summarize_uploads(results: dict) -> str:
    uploaded = [k for k, v in results.items() if v == "uploaded"]
    if uploaded:
        return f"uploaded: {', '.join(uploaded)}"
    return "no changes"


def main():
    s3 = s3_client()
    download_artifacts_from_r2(s3)
    process_steam_activity()
    return upload_artifacts_to_r2(s3)


if __name__ == "__main__":
    run_ts = int(time.time())
    try:
        upload_results = main()
        summary = summarize_uploads(upload_results)
        if "uploaded" in upload_results.values():
            send_discord_alert(
                f"Steam Activity (GitHub) — R2 sync at <t:{run_ts}:f> | {summary}"
            )
        else:
            send_discord_alert(
                f"Steam Activity (GitHub) — no changes to upload at <t:{run_ts}:f>"
            )
    except Exception as e:
        mention = f"<@{DISCORD_USER_ID}> " if DISCORD_USER_ID else ""
        send_discord_alert(format_failure_for_discord(mention, run_ts, e))
        print("Steam Activity (GitHub) failed. Details were sent to Discord.")
        sys.exit(1)
