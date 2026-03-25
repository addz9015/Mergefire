from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from integrations.github_client import GitHubClient
from integrations.groq_client import GroqClient

LOGGER = logging.getLogger(__name__)


class ReviewCopilotAgent:
    def __init__(
        self,
        github_client: GitHubClient,
        groq_client: GroqClient,
        prompts_dir: Path,
    ) -> None:
        self._github_client = github_client
        self._groq_client = groq_client
        self._security_prompt = (prompts_dir / "security_review.txt").read_text(encoding="utf-8")
        self._quality_prompt = (prompts_dir / "code_quality.txt").read_text(encoding="utf-8")

    async def run(self, repo_full_name: str, pr_number: int) -> dict[str, Any]:
        diff_text = await asyncio.to_thread(self._github_client.get_diff, repo_full_name, pr_number)
        context = await asyncio.to_thread(self._github_client.get_pr_context, repo_full_name, pr_number)
        changed_files = await asyncio.to_thread(self._github_client.get_changed_files, repo_full_name, pr_number)
        repo_facts = await asyncio.to_thread(
            self._github_client.get_repository_facts,
            repo_full_name,
            context.base_ref,
        )

        valid_lines = _build_valid_lines_by_file(changed_files)
        pr_body_for_analysis = _build_analysis_context(context.body, changed_files, repo_facts)
        analysis = await asyncio.to_thread(
            self._groq_client.analyze_diff,
            diff_text,
            self._security_prompt,
            self._quality_prompt,
            context.title,
            pr_body_for_analysis,
        )

        review_comments, dropped_notes = _prepare_inline_comments(analysis.get("comments", []), valid_lines)
        summary = _build_summary(
            ai_summary=str(analysis.get("summary", "")).strip(),
            posted_count=len(review_comments),
            dropped_notes=dropped_notes,
        )
        event = str(analysis.get("event", "COMMENT")).upper().strip() or "COMMENT"

        await asyncio.to_thread(
            self._github_client.post_inline_review,
            repo_full_name,
            pr_number,
            review_comments,
            summary,
            event,
        )

        return {
            "agent": "review_copilot",
            "event": event,
            "posted_comments": len(review_comments),
            "dropped_comments": len(dropped_notes),
            "highest_severity": _highest_severity(analysis.get("comments", [])),
        }


def _build_analysis_context(pr_body: str, changed_files: list[dict[str, Any]], repo_facts: str) -> str:
    changed_paths = [str(entry.get("filename", "")).strip() for entry in changed_files]
    changed_paths = [path for path in changed_paths if path]

    lines = [pr_body.strip()]
    lines.append("\n[Changed files]")
    if changed_paths:
        lines.extend(f"- {path}" for path in changed_paths[:200])
        if len(changed_paths) > 200:
            lines.append(f"- ... and {len(changed_paths) - 200} more")
    else:
        lines.append("- (none)")

    lines.append("\n[Repository facts]")
    lines.append(repo_facts.strip() or "(unavailable)")

    return "\n".join(lines).strip()


def _prepare_inline_comments(
    raw_comments: Any,
    valid_lines_by_file: dict[str, set[int]],
) -> tuple[list[dict[str, Any]], list[str]]:
    comments: list[dict[str, Any]] = []
    dropped_notes: list[str] = []

    if not isinstance(raw_comments, list):
        return comments, dropped_notes

    for item in raw_comments:
        if not isinstance(item, dict):
            continue

        path_input = str(item.get("path", "")).strip()
        resolved_path = _resolve_path(path_input, valid_lines_by_file)
        if not resolved_path:
            dropped_notes.append(f"Skipped finding for unknown file: {path_input}")
            continue

        line = _normalize_line(item.get("line"), valid_lines_by_file[resolved_path])
        if line is None:
            dropped_notes.append(f"Skipped finding for {resolved_path}: no valid inline line available")
            continue

        severity = str(item.get("severity", "MEDIUM")).upper().strip()
        category = str(item.get("category", "quality")).lower().strip()
        message = str(item.get("message", "")).strip()
        suggestion = str(item.get("suggestion", "")).strip()

        if not message:
            continue

        body_lines = [f"[{severity}] [{category}] {message}"]
        if suggestion:
            body_lines.append(f"Suggestion: {suggestion}")

        comments.append(
            {
                "path": resolved_path,
                "line": line,
                "body": "\n\n".join(body_lines),
            }
        )

    return comments, dropped_notes


def _resolve_path(path_input: str, valid_lines_by_file: dict[str, set[int]]) -> str | None:
    if path_input in valid_lines_by_file:
        return path_input

    normalized = path_input.replace("\\", "/").lstrip("./")
    if normalized.startswith("a/") or normalized.startswith("b/"):
        normalized = normalized[2:]
    if normalized in valid_lines_by_file:
        return normalized

    basename = Path(normalized).name
    matches = [path for path in valid_lines_by_file if Path(path).name == basename]
    if len(matches) == 1:
        return matches[0]

    return None


def _normalize_line(line_raw: Any, valid_lines: set[int]) -> int | None:
    if not valid_lines:
        return None

    try:
        line = int(line_raw)
    except (TypeError, ValueError):
        line = None

    if line is not None and line in valid_lines:
        return line

    if line is None:
        return min(valid_lines)

    return min(valid_lines, key=lambda candidate: abs(candidate - line))


def _build_valid_lines_by_file(changed_files: list[dict[str, Any]]) -> dict[str, set[int]]:
    mapping: dict[str, set[int]] = {}
    for changed_file in changed_files:
        filename = str(changed_file.get("filename", "")).strip()
        patch = str(changed_file.get("patch", "") or "")
        if not filename:
            continue

        lines = _extract_added_lines(patch)
        mapping[filename] = lines

    return mapping


def _extract_added_lines(patch: str) -> set[int]:
    added_lines: set[int] = set()
    current_new_line: int | None = None

    for line in patch.splitlines():
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,\d+)?", line)
            current_new_line = int(match.group(1)) if match else None
            continue

        if current_new_line is None:
            continue

        if line.startswith("+++"):
            continue

        if line.startswith("+"):
            added_lines.add(current_new_line)
            current_new_line += 1
            continue

        if line.startswith("-"):
            continue

        current_new_line += 1

    return added_lines


def _build_summary(ai_summary: str, posted_count: int, dropped_notes: list[str]) -> str:
    lines = ["### PR Review Copilot", ""]
    if ai_summary:
        lines.append(ai_summary)
        lines.append("")

    lines.append(f"Posted inline comments: {posted_count}")

    if dropped_notes:
        lines.append("")
        lines.append("Additional notes:")
        for note in dropped_notes[:10]:
            lines.append(f"- {note}")
        if len(dropped_notes) > 10:
            lines.append(f"- ... and {len(dropped_notes) - 10} more")

    return "\n".join(lines)


def _highest_severity(raw_comments: Any) -> str:
    if not isinstance(raw_comments, list):
        return "LOW"

    ranking = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
    highest = "LOW"
    for item in raw_comments:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "LOW")).upper().strip()
        if severity not in ranking:
            continue
        if ranking[severity] > ranking[highest]:
            highest = severity

    return highest
