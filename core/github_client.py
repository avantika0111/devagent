"""
core/github_client.py

Single GitHub API wrapper used by every agent.
All agents import this - none of them import PyGithub directly.

This means GitHub API logic lives in one place.
If the API changes or you swap libraries, you update one file.

Usage:
    client = GitHubClient(token=os.environ["GITHUB_TOKEN"], repo="you/project")
    diff   = client.get_pr_diff(42)
    files  = client.get_pr_files(42)
    client.post_comment(42, "LGTM")
    client.post_review_comment(42, "auth.py", 47, "Use parameterised queries.")
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from typing import Optional

from github import Github, GithubException
from github.PullRequest import PullRequest
from github.Repository import Repository


# ---------------------------------------------------------------------------
# Data classes returned by the client
# (agents work with these, never with raw PyGithub objects)
# ---------------------------------------------------------------------------

@dataclass
class PRFile:
    filename:    str
    status:      str        # added | modified | removed | renamed
    patch:       str        # unified diff for this file
    additions:   int
    deletions:   int
    sha:         str


@dataclass
class PRInfo:
    number:      int
    title:       str
    body:        str
    head_branch: str
    base_branch: str
    author:      str
    state:       str        # open | closed | merged
    draft:       bool
    url:         str


@dataclass
class FileContent:
    path:    str
    content: str
    sha:     str            # needed for updates
    branch:  str


@dataclass
class CommitResult:
    path:    str
    sha:     str
    url:     str
    branch:  str


@dataclass
class PRResult:
    number: int
    url:    str
    title:  str


# ---------------------------------------------------------------------------
# GitHubClient
# ---------------------------------------------------------------------------

class GitHubClient:
    """
    Wraps PyGithub for DevAgent's needs.

    All methods:
    - Return typed dataclasses, not raw PyGithub objects
    - Raise GitHubClientError (not GithubException) on failure
    - Retry on rate limits automatically
    """

    def __init__(self, token: str, repo: str):
        """
        Args:
            token: GitHub personal access token or app token
            repo:  full repo name, e.g. "yourusername/devagent"
        """
        self._gh:   Github     = Github(token)
        self._repo: Repository = self._get_repo(repo)
        self.repo_name = repo

    # ------------------------------------------------------------------
    # Pull request - read
    # ------------------------------------------------------------------

    def get_pr_info(self, pr_number: int) -> PRInfo:
        """Get metadata about a pull request."""
        pr = self._get_pr(pr_number)
        return PRInfo(
            number=pr.number,
            title=pr.title,
            body=pr.body or "",
            head_branch=pr.head.ref,
            base_branch=pr.base.ref,
            author=pr.user.login,
            state=pr.state,
            draft=pr.draft,
            url=pr.html_url,
        )

    def get_pr_files(self, pr_number: int) -> list[PRFile]:
        """Get all files changed in a PR with their diffs."""
        pr = self._get_pr(pr_number)
        files = []
        for f in pr.get_files():
            files.append(PRFile(
                filename=f.filename,
                status=f.status,
                patch=f.patch or "",
                additions=f.additions,
                deletions=f.deletions,
                sha=f.sha,
            ))
        return files

    def get_pr_diff(self, pr_number: int) -> str:
        """
        Get the full unified diff for a PR as a single string.
        Suitable for passing directly into an agent's context.
        """
        files = self.get_pr_files(pr_number)
        if not files:
            return ""

        sections: list[str] = []
        for f in files:
            header = f"### {f.status.upper()}: {f.filename}"
            if f.patch:
                sections.append(f"{header}\n```diff\n{f.patch}\n```")
            else:
                sections.append(f"{header}\n(binary file or no diff available)")

        return "\n\n".join(sections)

    def get_pr_changed_filenames(self, pr_number: int) -> list[str]:
        """Quick list of filenames changed in a PR - no diff content."""
        return [f.filename for f in self.get_pr_files(pr_number)]

    # ------------------------------------------------------------------
    # Pull request - write
    # ------------------------------------------------------------------

    def post_comment(self, pr_number: int, body: str) -> None:
        """Post a general comment on a PR (not line-level)."""
        pr = self._get_pr(pr_number)
        self._retry(lambda: pr.create_issue_comment(body))

    def post_review_comment(
        self,
        pr_number: int,
        file_path: str,
        line_number: int,
        body: str,
        side: str = "RIGHT",   # RIGHT = new file, LEFT = old file
    ) -> None:
        """
        Post a line-level review comment on a specific file and line.

        Args:
            pr_number:   PR number
            file_path:   relative path to the file, e.g. "src/auth.py"
            line_number: line number in the new version of the file
            body:        comment text
            side:        "RIGHT" for new code, "LEFT" for removed code
        """
        pr = self._get_pr(pr_number)
        # Get the latest commit on the PR head - required for review comments
        commit = list(pr.get_commits())[-1]

        try:
            self._retry(lambda: pr.create_review_comment(
                body=body,
                commit=commit,
                path=file_path,
                line=line_number,
                side=side,
            ))
        except GithubException as e:
            # Line-level comments fail if the line isn't in the diff.
            # Fall back to a general comment that references the location.
            fallback = f"**{file_path}:{line_number}**\n\n{body}"
            self.post_comment(pr_number, fallback)

    def post_review(
        self,
        pr_number: int,
        body: str,
        event: str = "COMMENT",  # APPROVE | REQUEST_CHANGES | COMMENT
    ) -> None:
        """
        Submit a formal PR review (approve / request changes / comment).

        Args:
            pr_number: PR number
            body:      review summary text
            event:     "APPROVE", "REQUEST_CHANGES", or "COMMENT"
        """
        pr = self._get_pr(pr_number)
        commit = list(pr.get_commits())[-1]
        self._retry(lambda: pr.create_review(
            commit=commit,
            body=body,
            event=event,
        ))

    def add_label(self, pr_number: int, label: str) -> None:
        """Add a label to a PR. Creates the label on the repo if it doesn't exist."""
        pr = self._get_pr(pr_number)
        try:
            self._repo.get_label(label)
        except GithubException:
            # Label doesn't exist yet - create it
            self._repo.create_label(label, color="ededed")
        self._retry(lambda: pr.add_to_labels(label))

    def create_pr(
        self,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str = "main",
        draft: bool = False,
    ) -> PRResult:
        """
        Create a new pull request.

        Returns PRResult with the PR number and URL.
        """
        pr = self._retry(lambda: self._repo.create_pull(
            title=title,
            body=body,
            head=head_branch,
            base=base_branch,
            draft=draft,
        ))
        return PRResult(number=pr.number, url=pr.html_url, title=pr.title)

    # ------------------------------------------------------------------
    # Files - read
    # ------------------------------------------------------------------

    def get_file(self, file_path: str, branch: str = "main") -> FileContent:
        """
        Read a file from the repository.

        Args:
            file_path: relative path, e.g. "src/auth.py"
            branch:    branch name (default: main)

        Raises:
            GitHubClientError: if the file doesn't exist
        """
        try:
            content = self._retry(lambda: self._repo.get_contents(file_path, ref=branch))
        except GithubException as e:
            raise GitHubClientError(f"File not found: {file_path} on {branch}") from e

        if isinstance(content, list):
            raise GitHubClientError(f"Path is a directory, not a file: {file_path}")

        return FileContent(
            path=file_path,
            content=content.decoded_content.decode("utf-8"),
            sha=content.sha,
            branch=branch,
        )

    def file_exists(self, file_path: str, branch: str = "main") -> bool:
        """Check if a file exists without raising on missing."""
        try:
            self.get_file(file_path, branch)
            return True
        except GitHubClientError:
            return False

    def list_files(
        self,
        directory: str = "",
        branch: str = "main",
        recursive: bool = False,
    ) -> list[str]:
        """
        List files in a directory.

        Args:
            directory: path to directory (empty string = repo root)
            branch:    branch name
            recursive: if True, recurse into subdirectories

        Returns list of relative file paths.
        """
        try:
            contents = self._retry(
                lambda: self._repo.get_contents(directory or "", ref=branch)
            )
        except GithubException as e:
            raise GitHubClientError(f"Cannot list {directory}: {e}") from e

        if not isinstance(contents, list):
            return [contents.path]

        paths: list[str] = []
        for item in contents:
            if item.type == "file":
                paths.append(item.path)
            elif item.type == "dir" and recursive:
                paths.extend(self.list_files(item.path, branch, recursive=True))

        return sorted(paths)

    # ------------------------------------------------------------------
    # Files - write
    # ------------------------------------------------------------------

    def commit_file(
        self,
        file_path: str,
        content: str,
        commit_message: str,
        branch: str,
    ) -> CommitResult:
        """
        Create or update a file on a branch.

        Handles both new files (create) and existing files (update) transparently.
        Content is a plain string - encoding is handled internally.
        """
        encoded = content.encode("utf-8")

        try:
            existing = self.get_file(file_path, branch)
            # File exists - update it
            result = self._retry(lambda: self._repo.update_file(
                path=file_path,
                message=commit_message,
                content=encoded,
                sha=existing.sha,
                branch=branch,
            ))
        except GitHubClientError:
            # File doesn't exist - create it
            result = self._retry(lambda: self._repo.create_file(
                path=file_path,
                message=commit_message,
                content=encoded,
                branch=branch,
            ))

        commit = result["commit"]
        return CommitResult(
            path=file_path,
            sha=commit.sha,
            url=commit.html_url,
            branch=branch,
        )

    # ------------------------------------------------------------------
    # Branches
    # ------------------------------------------------------------------

    def create_branch(self, branch_name: str, from_branch: str = "main") -> str:
        """
        Create a new branch from an existing branch.

        Returns the SHA of the source commit.
        Raises GitHubClientError if the source branch doesn't exist.
        """
        try:
            source = self._retry(lambda: self._repo.get_branch(from_branch))
        except GithubException as e:
            raise GitHubClientError(f"Source branch not found: {from_branch}") from e

        try:
            self._retry(lambda: self._repo.create_git_ref(
                ref=f"refs/heads/{branch_name}",
                sha=source.commit.sha,
            ))
        except GithubException as e:
            if "already exists" in str(e).lower():
                raise GitHubClientError(
                    f"Branch already exists: {branch_name}"
                ) from e
            raise GitHubClientError(str(e)) from e

        return source.commit.sha

    def branch_exists(self, branch_name: str) -> bool:
        """Check if a branch exists without raising."""
        try:
            self._repo.get_branch(branch_name)
            return True
        except GithubException:
            return False

    def get_default_branch(self) -> str:
        """Get the repo's default branch name (usually 'main' or 'master')."""
        return self._repo.default_branch

    # ------------------------------------------------------------------
    # Repository info
    # ------------------------------------------------------------------

    def get_repo_languages(self) -> dict[str, int]:
        """
        Get languages used in the repo (from GitHub's detection).
        Returns dict of language -> byte count, e.g. {"Python": 42000, "YAML": 1200}
        """
        return dict(self._repo.get_languages())

    def get_repo_topics(self) -> list[str]:
        """Get GitHub topics set on the repository."""
        return list(self._repo.get_topics())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_repo(self, repo_name: str) -> Repository:
        try:
            return self._gh.get_repo(repo_name)
        except GithubException as e:
            raise GitHubClientError(
                f"Cannot access repository '{repo_name}'. "
                f"Check your GITHUB_TOKEN has repo scope. Error: {e}"
            ) from e

    def _get_pr(self, pr_number: int) -> PullRequest:
        try:
            return self._repo.get_pull(pr_number)
        except GithubException as e:
            raise GitHubClientError(
                f"PR #{pr_number} not found in {self.repo_name}"
            ) from e

    @staticmethod
    def _retry(fn, max_attempts: int = 3, base_delay: float = 1.0):
        """
        Retry a GitHub API call on rate limit or transient errors.
        Exponential backoff: 1s, 2s, 4s.
        """
        for attempt in range(max_attempts):
            try:
                return fn()
            except GithubException as e:
                status = e.status if hasattr(e, "status") else 0

                if status == 403:
                    # Rate limited - check reset time
                    reset_time = e.headers.get("X-RateLimit-Reset") if hasattr(e, "headers") else None
                    if reset_time:
                        wait = max(0, int(reset_time) - int(time.time())) + 1
                        time.sleep(min(wait, 60))  # cap at 60s
                    elif attempt < max_attempts - 1:
                        time.sleep(base_delay * (2 ** attempt))
                    continue

                if status in (500, 502, 503, 504) and attempt < max_attempts - 1:
                    # Transient server error - retry with backoff
                    time.sleep(base_delay * (2 ** attempt))
                    continue

                raise  # non-retryable error

        raise GitHubClientError(f"Failed after {max_attempts} attempts")


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class GitHubClientError(Exception):
    """
    All GitHub errors are re-raised as GitHubClientError.
    Agents catch this - not GithubException directly.
    This decouples agents from PyGithub's exception hierarchy.
    """
    pass


# ---------------------------------------------------------------------------
# Factory - reads token from environment
# ---------------------------------------------------------------------------

def create_github_client(repo: str) -> GitHubClient:
    """
    Create a GitHubClient using GITHUB_TOKEN from the environment.

    Raises:
        EnvironmentError: if GITHUB_TOKEN is not set
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError(
            "GITHUB_TOKEN environment variable is not set. "
            "Create a token at github.com/settings/tokens with repo scope."
        )
    return GitHubClient(token=token, repo=repo)
