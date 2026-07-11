"""archive.fo capture client and HTML validation."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import quote

from bs4 import BeautifulSoup

_CHALLENGE_PATTERNS = re.compile(
    r"Security Verification|captcha|challenge|access denied|please verify",
    re.IGNORECASE,
)

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class ArchiveValidationResult:
    valid: bool
    reason: str | None
    word_count: int
    title: str | None
    byline: str | None


def validate_archive_capture(
    html_text: str,
    expected_title: str | None = None,
) -> ArchiveValidationResult:
    """Validate an archive.fo capture for quality and completeness."""
    soup = BeautifulSoup(html_text, "html.parser")

    # Check for challenge / block page
    body_text = soup.get_text(separator=" ", strip=True)
    if _CHALLENGE_PATTERNS.search(body_text):
        return ArchiveValidationResult(
            valid=False,
            reason="challenge_or_block_page",
            word_count=0,
            title=None,
            byline=None,
        )

    # Try to find the article container
    article = soup.find("article") or soup.find("main")
    if article is None:
        return ArchiveValidationResult(
            valid=False,
            reason="no_article_element",
            word_count=0,
            title=None,
            byline=None,
        )

    # Extract title
    title = None
    h1 = article.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    elif soup.title:
        title = soup.title.get_text(strip=True)

    # Extract byline
    byline = None
    byline_el = (
        article.find(class_=re.compile(r"byline|author", re.IGNORECASE))
        or article.find("span", class_=re.compile(r"author", re.IGNORECASE))
    )
    if byline_el:
        byline = byline_el.get_text(strip=True)

    # Extract body text (paragraphs inside article)
    paragraphs = article.find_all("p")
    body_words = " ".join(p.get_text(strip=True) for p in paragraphs)
    word_count = len(body_words.split())

    # Reject if too short
    if word_count < 20:
        return ArchiveValidationResult(
            valid=False,
            reason="body_too_short",
            word_count=word_count,
            title=title,
            byline=byline,
        )

    # Title mismatch check
    if expected_title and title:
        # Simple normalised comparison
        def _norm(s: str) -> str:
            return re.sub(r"\s+", " ", s.strip().lower())

        if _norm(expected_title) not in _norm(title) and _norm(title) not in _norm(expected_title):
            return ArchiveValidationResult(
                valid=False,
                reason="title_mismatch",
                word_count=word_count,
                title=title,
                byline=byline,
            )

    return ArchiveValidationResult(
        valid=True,
        reason=None,
        word_count=word_count,
        title=title,
        byline=byline,
    )


class ArchiveFoClient:
    """Client for submitting, polling, and downloading archive.fo captures."""

    def __init__(
        self,
        archive_host: str = "https://archive.fo",
        request_fn=None,
        timeout: int = 30,
        poll_interval: int = 10,
        max_polls: int = 12,
        user_agent: str = _DEFAULT_USER_AGENT,
    ):
        self.archive_host = archive_host.rstrip("/")
        self.request_fn = request_fn
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        self.user_agent = user_agent

    def _do_request(self, method: str, url: str, **kwargs) -> object:
        """Delegate to the injected request function or raise."""
        if self.request_fn is None:
            raise RuntimeError("No request_fn provided. Inject one for network access.")
        return self.request_fn(method, url, timeout=self.timeout, **kwargs)

    def submit(self, url: str) -> str:
        """Submit a URL to archive.fo and return the archive redirect URL."""
        submit_url = f"{self.archive_host}/submit/?url={quote(url, safe='')}"
        response = self._do_request(
            "GET",
            submit_url,
            headers={"User-Agent": self.user_agent},
            allow_redirects=False,
        )

        # archive.fo returns 302 with Location header to the archive page
        location = getattr(response, "headers", {}).get("Location", "")
        if not location:
            # Some responses return the archive URL in the body or as a meta refresh
            text = getattr(response, "text", "") or ""
            match = re.search(r"https?://archive\.\w+/\w+", text)
            if match:
                location = match.group(0)

        if not location:
            raise RuntimeError("archive.fo did not return an archive URL")

        return location

    def poll(self, archive_url: str) -> str:
        """Poll until the capture is ready. Returns the archive URL."""
        for _ in range(self.max_polls):
            response = self._do_request(
                "GET",
                archive_url,
                headers={"User-Agent": self.user_agent},
                allow_redirects=True,
            )
            status = getattr(response, "status_code", getattr(response, "status", 0))
            if status == 200:
                return archive_url
            time.sleep(self.poll_interval)

        raise TimeoutError(
            f"archive.fo capture not ready after {self.max_polls} polls "
            f"({self.max_polls * self.poll_interval}s)"
        )

    def download(self, archive_url: str) -> str:
        """Download the captured HTML from archive.fo."""
        response = self._do_request(
            "GET",
            archive_url,
            headers={"User-Agent": self.user_agent},
            allow_redirects=True,
        )
        text = getattr(response, "text", None)
        if text is None:
            raise RuntimeError("Empty response from archive.fo")
        return text
