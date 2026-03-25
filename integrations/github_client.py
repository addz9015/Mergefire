from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from github import Github
from github.GithubException import GithubException, UnknownObjectException

LOGGER = logging.getLogger(__name__)

RISK_LABELS = ("risk:low", "risk:medium", "risk:high")


@dataclass
class PRContext:
    repo_full_name: str
    number: int
    title: str
    body: str
    html_url: str
    head_sha: str
    base_ref: str
    author: str


class GitHubClient:
    def __init__(self, token: str, timeout_seconds: float = 25.0) -> None:
        self._gh = Github(login_or_token=token, per_page=100)
        self._http = httpx.Client(
            timeout=timeout_seconds,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pr-review-copilot-risk-radar",
            },
        )

    def close(self) -> None:
        self._http.close()

    def _repo(self, repo_full_name: str):
        return self._gh.get_repo(repo_full_name)

    def _pr(self, repo_full_name: str, pr_number: int):
        return self._repo(repo_full_name).get_pull(pr_number)

    def get_pr_context(self, repo_full_name: str, pr_number: int) -> PRContext:
        pr = self._pr(repo_full_name, pr_number)
        return PRContext(
            repo_full_name=repo_full_name,
            number=pr_number,
            title=pr.title or "",
            body=pr.body or "",
            html_url=pr.html_url,
            head_sha=pr.head.sha,
            base_ref=pr.base.ref,
            author=pr.user.login if pr.user else "unknown",
        )

    def get_diff(self, repo_full_name: str, pr_number: int) -> str:
        pr = self._pr(repo_full_name, pr_number)
        response = self._http.get(
            pr.diff_url,
            headers={"Accept": "application/vnd.github.v3.diff"},
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.text

    def get_changed_files(self, repo_full_name: str, pr_number: int) -> list[dict[str, Any]]:
        pr = self._pr(repo_full_name, pr_number)
        files: list[dict[str, Any]] = []
        for changed in pr.get_files():
            files.append(
                {
                    "filename": changed.filename,
                    "status": changed.status,
                    "additions": changed.additions,
                    "deletions": changed.deletions,
                    "changes": changed.changes,
                    "patch": changed.patch or "",
                }
            )
        return files

    def get_repository_facts(self, repo_full_name: str, ref: str | None = None) -> str:
        repo = self._repo(repo_full_name)

        try:
            root_entries = repo.get_contents("", ref=ref)
        except GithubException:
            root_entries = []

        if not isinstance(root_entries, list):
            root_entries = [root_entries]

        top_files: list[str] = []
        top_dirs: list[str] = []
        for entry in root_entries:
            entry_type = str(getattr(entry, "type", "")).lower()
            entry_name = str(getattr(entry, "name", "")).strip()
            if not entry_name:
                continue
            if entry_type == "dir":
                top_dirs.append(entry_name)
            else:
                top_files.append(entry_name)

        languages = list(repo.get_languages().keys())[:8]

        important_files = [
            "README.md",
            "requirements.txt",
            "pyproject.toml",
            "package.json",
            "Dockerfile",
            "main.py",
            "app.py",
        ]
        snippets: list[str] = []
        for path in important_files:
            snippet = self._read_file_excerpt(repo_full_name, path, ref=ref, max_chars=800)
            if snippet:
                snippets.append(f"--- {path} ---\n{snippet}")

        lines = [
            f"Repository: {repo.full_name}",
            f"Ref: {ref or repo.default_branch}",
            f"Primary languages: {', '.join(languages) if languages else 'unknown'}",
            f"Top-level directories: {', '.join(sorted(top_dirs)[:25]) if top_dirs else '(none)'}",
            f"Top-level files: {', '.join(sorted(top_files)[:25]) if top_files else '(none)'}",
        ]
        if snippets:
            lines.append("Important file excerpts:")
            lines.extend(snippets)

        return "\n".join(lines)

    def _read_file_excerpt(
        self,
        repo_full_name: str,
        path: str,
        ref: str | None = None,
        max_chars: int = 1200,
    ) -> str:
        try:
            content = self._repo(repo_full_name).get_contents(path, ref=ref)
        except GithubException:
            return ""

        if isinstance(content, list):
            return ""

        try:
            decoded = content.decoded_content.decode("utf-8", errors="replace")
        except Exception:
            return ""

        text = decoded.strip()
        if not text:
            return ""
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"
        return text

    def post_issue_comment(self, repo_full_name: str, pr_number: int, body: str) -> None:
        self._pr(repo_full_name, pr_number).create_issue_comment(body=body)

    def post_inline_review(
        self,
        repo_full_name: str,
        pr_number: int,
        comments: list[dict[str, Any]],
        summary: str,
        event: str = "COMMENT",
    ) -> None:
        normalized_comments: list[dict[str, Any]] = []
        for item in comments:
            path = str(item.get("path", "")).strip()
            body = str(item.get("body", "")).strip()
            line_raw = item.get("line")
            if not path or not body or line_raw is None:
                continue
            try:
                line_value = int(line_raw)
            except (TypeError, ValueError):
                continue
            normalized_comments.append(
                {
                    "path": path,
                    "line": line_value,
                    "side": "RIGHT",
                    "body": body[:65535],
                }
            )

        if not normalized_comments:
            if summary.strip():
                self.post_issue_comment(repo_full_name, pr_number, summary)
            return

        context = self.get_pr_context(repo_full_name, pr_number)
        payload = {
            "commit_id": context.head_sha,
            "event": event,
            "body": summary[:65535] if summary.strip() else "Automated PR review.",
            "comments": normalized_comments,
        }
        response = self._http.post(
            f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/reviews",
            json=payload,
        )

        if response.status_code < 400:
            return

        LOGGER.warning(
            "Inline review failed for %s#%s (%s): %s",
            repo_full_name,
            pr_number,
            response.status_code,
            response.text,
        )
        fallback = self._build_fallback_review_text(summary, normalized_comments)
        self.post_issue_comment(repo_full_name, pr_number, fallback)

    def add_label(self, repo_full_name: str, pr_number: int, label: str) -> None:
        pr = self._pr(repo_full_name, pr_number)
        current_labels = {entry.name for entry in pr.get_labels()}
        if label not in current_labels:
            pr.add_to_labels(label)

    def clear_risk_labels(self, repo_full_name: str, pr_number: int) -> None:
        pr = self._pr(repo_full_name, pr_number)
        for label in RISK_LABELS:
            try:
                pr.remove_from_labels(label)
            except UnknownObjectException:
                continue
            except GithubException as exc:
                if getattr(exc, "status", None) == 404:
                    continue
                LOGGER.exception("Could not remove label %s from %s#%s", label, repo_full_name, pr_number)

    def set_required_reviewers(self, repo_full_name: str, pr_number: int, required_count: int = 2) -> bool:
        context = self.get_pr_context(repo_full_name, pr_number)
        branch = quote(context.base_ref, safe="")

        endpoint = (
            f"https://api.github.com/repos/{repo_full_name}/branches/{branch}"
            "/protection/required_pull_request_reviews"
        )
        payload = {
            "dismiss_stale_reviews": False,
            "require_code_owner_reviews": False,
            "required_approving_review_count": required_count,
            "require_last_push_approval": False,
        }

        response = self._http.patch(endpoint, json=payload)
        if response.status_code in (200, 201):
            return True

        LOGGER.warning(
            "Could not set required reviewers for %s (branch=%s). Status=%s Body=%s",
            repo_full_name,
            context.base_ref,
            response.status_code,
            response.text,
        )
        return False

    @staticmethod
    def _build_fallback_review_text(summary: str, comments: list[dict[str, Any]]) -> str:
        lines = [summary.strip() or "Automated review found comments:", "", "Inline comments fallback:"]
        for comment in comments[:50]:
            lines.append(f"- {comment['path']}:{comment['line']} -> {comment['body']}")
        if len(comments) > 50:
            lines.append(f"- ... and {len(comments) - 50} more comments")
        return "\n".join(lines)
