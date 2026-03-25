from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from typing import Any

import httpx
from dotenv import load_dotenv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send a signed GitHub webhook payload to a local PR Review Copilot service.",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8000/webhook",
        help="Webhook endpoint URL (default: http://localhost:8000/webhook)",
    )
    parser.add_argument(
        "--event",
        default="pull_request",
        help="GitHub event name (default: pull_request)",
    )
    parser.add_argument(
        "--action",
        default="opened",
        help="GitHub action (default: opened)",
    )
    parser.add_argument(
        "--repo",
        default="owner/repo",
        help="repository.full_name in payload (default: owner/repo)",
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=1,
        help="pull_request.number in payload (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds (default: 20)",
    )
    return parser


def _payload(action: str, repo_full_name: str, pr_number: int) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": repo_full_name},
        "pull_request": {"number": pr_number},
    }


def main() -> int:
    load_dotenv()
    args = _build_parser().parse_args()

    secret = (os.getenv("GITHUB_WEBHOOK_SECRET") or "").strip()
    if not secret:
        print("Missing GITHUB_WEBHOOK_SECRET in environment or .env")
        return 2

    payload = _payload(args.action, args.repo, args.pr)
    payload_bytes = json.dumps(payload).encode("utf-8")

    signature = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        msg=payload_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()

    headers = {
        "X-GitHub-Event": args.event,
        "X-Hub-Signature-256": signature,
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=args.timeout) as client:
        response = client.post(args.url, content=payload_bytes, headers=headers)

    print(f"Status: {response.status_code}")
    print(response.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
