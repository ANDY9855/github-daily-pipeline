"""GitHub Actions entry point for one-repository-per-day rotation."""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from src.github_api.client import GitHubClient
from src.logging.runlog import actions_summary, configure
from src.processor.engine import process
from src.queue.store import QueueStore


def main() -> int:
    logger = configure(os.environ.get("LOG_DIR", "logs"))
    token = os.environ.get("GITHUB_TOKEN", "").replace("\r", "").replace("\n", "").strip().strip('"').strip("'")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    username = os.environ.get("GITHUB_USERNAME", "").strip()
    if not token or not username:
        raise SystemExit("GITHUB_TOKEN and GITHUB_USERNAME are required")
    timezone_name = os.environ.get("TIMEZONE", "Asia/Karachi")
    now = datetime.now(ZoneInfo(timezone_name))
    slot = now.hour // 6  # 4 slots per day (every 6 hours: 00:00, 06:00, 12:00, 18:00)
    interval_key = f"{now.date().isoformat()}-slot-{slot}"
    display_timestamp = now.strftime("%Y-%m-%d %H:%M %Z")

    client = GitHubClient(token, username)
    store = QueueStore(os.environ.get("QUEUE_STATE_PATH", "state/repos.json"))
    automation_repo = os.environ.get("AUTOMATION_REPOSITORY", f"{username}/github-daily-pipeline")
    records = store.sync(client.repositories(), automation_repo)

    force_run = os.environ.get("FORCE_RUN", "false").lower() == "true"
    target = store.next_eligible() if force_run else store.select_for_day(interval_key)
    if target is None:
        message = f"No repository selected: the 6-hour rotation interval for {interval_key} is already complete or the queue is empty"
        logger.info(message)
        actions_summary("Repository rotation", [message, f"Total repositories: {len(records)}"])
        return 0

    dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"
    if not dry_run:
        store.begin_run(target, interval_key)
    logger.info("Interval=%s selected=%s position=%s/%s new=%s dry_run=%s", interval_key, target.full_name, target.queue_position, len(records), target.is_new, dry_run)
    try:
        status, action, commit_sha, push_result = process(target, dry_run, logger, client=client, run_date=display_timestamp)
        if dry_run:
            logger.info("Dry run complete; rotation state was not advanced")
            actions_summary("Repository rotation dry run", [
                f"Interval: `{interval_key}`", f"Timestamp: `{display_timestamp}`",
                f"Selected: `{target.full_name}` ({target.queue_position}/{len(records)})",
                f"Result: `{status}`", f"Action: {action}", "Rotation state: unchanged",
            ])
            return 0
        target.status = status
        next_position = next((r.queue_position for r in records if r.enabled and not r.manual_review and r.queue_position > target.queue_position), None)
        if next_position is None:
            next_position = min((r.queue_position for r in records if r.enabled and not r.manual_review), default=1)
        store.finish_run(target, status, action, interval_key, commit_sha=commit_sha, push_result=push_result)
        logger.info("Result=%s action=%s next_position=%s", status, action, next_position)
        actions_summary("Repository rotation", [
            f"Interval: `{interval_key}`", f"Timestamp: `{display_timestamp}`",
            f"Selected: `{target.full_name}` ({target.queue_position}/{len(records)})",
            f"New repository: `{target.is_new}`", f"Result: `{status}`", f"Action: {action}",
            f"Dry run: `{dry_run}`", f"Next rotation position: `{next_position}`",
        ])
    except Exception as error:
        if not dry_run:
            store.record_failure(target, interval_key, "Processing failed", str(error))
        logger.exception("Processing failed for %s", target.full_name)
        if dry_run:
            actions_summary("Repository rotation dry-run failure", [f"Interval: `{interval_key}`", f"Selected: `{target.full_name}`", f"Error: {error}", "Rotation state: unchanged"])
        else:
            actions_summary("Repository rotation failure", [f"Interval: `{interval_key}`", f"Selected: `{target.full_name}`", f"Failure count: `{target.failure_count}`", f"Manual review: `{target.manual_review}`", f"Error: {error}"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
