import logging
import os
import re
import subprocess
from urllib.parse import urljoin

from markdownify import markdownify as md
from readabilipy import simple_json_from_html_string

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_READABILITY_JS_DISABLED = os.getenv("DEERFLOW_DISABLE_READABILITY_JS", "").strip().lower() in _TRUE_VALUES
_READABILITY_JS_DISABLE_REASON = "disabled by DEERFLOW_DISABLE_READABILITY_JS" if _READABILITY_JS_DISABLED else ""
_READABILITY_JS_DISABLE_LOGGED = False


class Article:
    url: str

    def __init__(self, title: str, html_content: str):
        self.title = title
        self.html_content = html_content

    def to_markdown(self, including_title: bool = True) -> str:
        markdown = ""
        if including_title:
            markdown += f"# {self.title}\n\n"

        if self.html_content is None or not str(self.html_content).strip():
            markdown += "*No content available*\n"
        else:
            markdown += md(self.html_content)

        return markdown

    def to_message(self) -> list[dict]:
        image_pattern = r"!\[.*?\]\((.*?)\)"

        content: list[dict[str, str]] = []
        markdown = self.to_markdown()

        if not markdown or not markdown.strip():
            return [{"type": "text", "text": "No content available"}]

        parts = re.split(image_pattern, markdown)

        for i, part in enumerate(parts):
            if i % 2 == 1:
                image_url = urljoin(self.url, part.strip())
                content.append({"type": "image_url", "image_url": {"url": image_url}})
            else:
                text_part = part.strip()
                if text_part:
                    content.append({"type": "text", "text": text_part})

        # If after processing all parts, content is still empty, provide a fallback message.
        if not content:
            content = [{"type": "text", "text": "No content available"}]

        return content


class ReadabilityExtractor:
    def extract_article(self, html: str) -> Article:
        global _READABILITY_JS_DISABLED, _READABILITY_JS_DISABLE_REASON, _READABILITY_JS_DISABLE_LOGGED

        if _READABILITY_JS_DISABLED and not _READABILITY_JS_DISABLE_LOGGED:
            logger.info("Readability.js is disabled (%s); using pure-Python extraction", _READABILITY_JS_DISABLE_REASON)
            _READABILITY_JS_DISABLE_LOGGED = True

        try:
            if _READABILITY_JS_DISABLED:
                article = simple_json_from_html_string(html, use_readability=False)
            else:
                article = simple_json_from_html_string(html, use_readability=True)
        except (subprocess.CalledProcessError, FileNotFoundError, PermissionError, OSError) as exc:
            stderr = getattr(exc, "stderr", None)
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            stderr_text = stderr.strip() if isinstance(stderr, str) and stderr.strip() else ""
            reason = type(exc).__name__
            if stderr_text:
                reason = f"{reason}: {stderr_text[:200]}"

            _READABILITY_JS_DISABLED = True
            _READABILITY_JS_DISABLE_REASON = reason
            if not _READABILITY_JS_DISABLE_LOGGED:
                logger.warning(
                    "Readability.js extraction unavailable (%s); switching to pure-Python extraction for this process",
                    reason,
                )
                _READABILITY_JS_DISABLE_LOGGED = True

            article = simple_json_from_html_string(html, use_readability=False)

        html_content = article.get("content")
        if not html_content or not str(html_content).strip():
            html_content = "No content could be extracted from this page"

        title = article.get("title")
        if not title or not str(title).strip():
            title = "Untitled"

        return Article(title=title, html_content=html_content)
