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

# Default archive hosts to try in order.
# archive.ph/fo/today/li bypass FT paywall but share rate limits.
# web.archive.org is the fallback — gets the page but not paywall bypass.
_DEFAULT_ARCHIVE_HOSTS = [
    "https://archive.fo",
    "https://archive.ph",
    "https://archive.today",
    "https://archive.li",
    "https://web.archive.org",
]


class ArchiveRateLimitError(Exception):
    """Raised when archive.fo returns 429 Too Many Requests."""

    def __init__(self, host: str, retry_after: int | None = None):
        self.host = host
        self.retry_after = retry_after
        super().__init__(
            f"Rate limited by {host}"
            + (f" (retry after {retry_after}s)" if retry_after else "")
        )


class ArchiveCaptureError(Exception):
    """Raised when capture fails for non-rate-limit reasons."""


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
    """Validate an archive capture for quality and completeness."""
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
    """Client for submitting, polling, and downloading archive captures.

    Supports multiple archive hosts with automatic failover on rate limits.
    """

    def __init__(
        self,
        archive_host: str | None = None,
        archive_hosts: list[str] | None = None,
        request_fn=None,
        timeout: int = 30,
        poll_interval: int = 10,
        max_polls: int = 12,
        max_retries: int = 3,
        retry_backoff: int = 30,
        request_delay: float = 2.0,
        user_agent: str = _DEFAULT_USER_AGENT,
    ):
        # Support both single host (backward compat) and multi-host
        if archive_hosts:
            self.archive_hosts = [h.rstrip("/") for h in archive_hosts]
        elif archive_host:
            self.archive_hosts = [archive_host.rstrip("/")]
        else:
            self.archive_hosts = list(_DEFAULT_ARCHIVE_HOSTS)

        self.request_fn = request_fn
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.request_delay = request_delay
        self.user_agent = user_agent
        self._last_request_time = 0.0

    def _do_request(self, method: str, url: str, **kwargs) -> object:
        """Delegate to the injected request function or raise."""
        if self.request_fn is None:
            raise RuntimeError("No request_fn provided. Inject one for network access.")
        return self.request_fn(method, url, timeout=self.timeout, **kwargs)

    def _throttle(self):
        """Enforce minimum delay between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)
        self._last_request_time = time.time()

    def _check_rate_limit(self, response, host: str):
        """Raise ArchiveRateLimitError if response is 429."""
        status = getattr(response, "status_code", getattr(response, "status", 0))
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            raise ArchiveRateLimitError(
                host,
                retry_after=int(retry_after) if retry_after else None,
            )

    def submit(self, url: str) -> str:
        """Submit a URL to archive service and return the archive redirect URL.

        Tries each host in order. On 429, tries the next host.
        If all hosts are rate-limited, raises ArchiveRateLimitError.
        """
        last_error = None
        for host in self.archive_hosts:
            self._throttle()
            is_wayback = "web.archive.org" in host

            try:
                if is_wayback:
                    # archive.org: GET /save/<url> with follow redirects
                    submit_url = f"{host}/save/{url}"
                    response = self._do_request(
                        "GET",
                        submit_url,
                        headers={"User-Agent": self.user_agent},
                        allow_redirects=True,
                    )
                    status = getattr(response, "status_code", 0)
                    self._check_rate_limit(response, host)
                    # The final URL after redirects IS the archive URL
                    final_url = str(getattr(response, "url", ""))
                    if "web.archive.org/web/" in final_url:
                        return final_url
                else:
                    # archive.ph style: GET /submit/?url=<url> expecting 302
                    submit_url = f"{host}/submit/?url={quote(url, safe='')}"
                    response = self._do_request(
                        "GET",
                        submit_url,
                        headers={"User-Agent": self.user_agent},
                        allow_redirects=False,
                    )
                    self._check_rate_limit(response, host)

                    # Extract archive URL from redirect
                    location = getattr(response, "headers", {}).get("Location", "")
                    if not location:
                        text = getattr(response, "text", "") or ""
                        match = re.search(r"https?://archive\.\w+/\w+", text)
                        if match:
                            location = match.group(0)
                    if location:
                        return location
            except ArchiveRateLimitError:
                last_error = host
                continue

        # All hosts tried — distinguish rate limit from other failures
        if last_error:
            raise ArchiveRateLimitError(last_error)
        raise ArchiveCaptureError(
            f"No archive URL returned from any host: {', '.join(self.archive_hosts)}"
        )

    def poll(self, archive_url: str) -> str:
        """Poll until the capture is ready. Returns the archive URL."""
        for _ in range(self.max_polls):
            self._throttle()
            response = self._do_request(
                "GET",
                archive_url,
                headers={"User-Agent": self.user_agent},
                allow_redirects=True,
            )
            status = getattr(response, "status_code", getattr(response, "status", 0))
            if status == 200:
                return archive_url
            if status == 429:
                # Rate limited during polling — wait longer
                time.sleep(self.retry_backoff)
                continue
            time.sleep(self.poll_interval)

        raise TimeoutError(
            f"archive capture not ready after {self.max_polls} polls "
            f"({self.max_polls * self.poll_interval}s)"
        )

    def download(self, archive_url: str) -> str:
        """Download the captured HTML."""
        self._throttle()
        response = self._do_request(
            "GET",
            archive_url,
            headers={"User-Agent": self.user_agent},
            allow_redirects=True,
        )
        status = getattr(response, "status_code", getattr(response, "status", 0))
        if status == 429:
            raise ArchiveRateLimitError("download")
        text = getattr(response, "text", None)
        if text is None:
            raise ArchiveCaptureError("Empty response from archive service")
        return text
