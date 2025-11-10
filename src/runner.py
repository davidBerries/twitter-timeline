import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

from extractors.twitter_parser import TimelineFetcher
from outputs.exporters import Exporter
from extractors.utils_time import now_utc_iso

# Project root resolution (works when run from repo or any cwd)
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "src" / "config"
DEFAULT_INPUTS = DATA_DIR / "inputs.sample.txt"
DEFAULT_OUTPUT = DATA_DIR / "sample.json"
DEFAULT_SETTINGS = CONFIG_DIR / "settings.example.json"

def setup_logging(verbosity: int = 1) -> None:
    level = logging.WARNING
    if verbosity >= 2:
        level = logging.INFO
    if verbosity >= 3:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

def load_settings(settings_path: Optional[Path]) -> dict:
    # Environment first
    cfg = {
        "max_posts_per_user": int(os.getenv("MAX_POSTS_PER_USER", "50")),
        "concurrency": int(os.getenv("CONCURRENCY", "5")),
        "output_path": os.getenv("OUTPUT_PATH", str(DEFAULT_OUTPUT)),
        "ndjson": os.getenv("NDJSON", "false").lower() == "true",
        "timeout_seconds": float(os.getenv("HTTP_TIMEOUT", "20")),
        "proxy": os.getenv("HTTP_PROXY") or os.getenv("http_proxy"),
        "nitter_base": os.getenv("NITTER_BASE", "https://nitter.net"),
    }
    # Override with file if exists
    if settings_path and settings_path.exists():
        try:
            file_cfg = json.loads(settings_path.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in file_cfg.items() if v is not None})
        except Exception as e:
            logging.getLogger("runner").warning(
                "Failed to read settings file %s: %s", settings_path, e
            )
    return cfg

def load_usernames(args: List[str]) -> List[str]:
    # 1) CLI usernames (comma or space separated)
    cli_names: List[str] = []
    for a in args:
        cli_names.extend([p.strip() for p in a.split(",") if p.strip()])
    if cli_names:
        return list(dict.fromkeys(cli_names))  # dedupe, preserve order

    # 2) Fallback to file
    path = DEFAULT_INPUTS
    if not path.exists():
        logging.getLogger("runner").warning(
            "No CLI usernames provided and %s missing. Using sample accounts.", path
        )
        return ["jack", "Twitter", "elonmusk"]
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.append(line)
    if not names:
        names = ["jack"]
    return list(dict.fromkeys(names))

async def fetch_all(usernames: List[str], cfg: dict) -> List[dict]:
    fetcher = TimelineFetcher(
        nitter_base=cfg["nitter_base"],
        timeout_seconds=cfg["timeout_seconds"],
        proxy=cfg.get("proxy"),
        max_posts=cfg["max_posts_per_user"],
    )
    sem = asyncio.Semaphore(cfg["concurrency"])
    results: List[dict] = []

    async def _task(name: str):
        async with sem:
            try:
                items = await fetcher.fetch_username(name)
                results.extend(items)
                logging.info("Fetched %d posts for @%s", len(items), name)
            except Exception as e:
                logging.exception("Failed fetching @%s: %s", name, e)

    await asyncio.gather(*[_task(u) for u in usernames])
    # Attach run metadata
    for obj in results:
        obj.setdefault("_fetched_at", now_utc_iso())
        obj.setdefault("_source", "nitter")
    return results

def main():
    setup_logging(int(os.getenv("LOG_LEVEL", "2")))
    cfg = load_settings(DEFAULT_SETTINGS if DEFAULT_SETTINGS.exists() else None)
    usernames = load_usernames(sys.argv[1:])
    logging.info("Targets: %s", ", ".join(usernames))

    items = asyncio.run(fetch_all(usernames, cfg))
    Exporter(
        output_path=Path(cfg["output_path"]),
        ndjson=cfg["ndjson"],
    ).write(items)

    print(f"Wrote {len(items)} records to {cfg['output_path']}")

if __name__ == "__main__":
    main()