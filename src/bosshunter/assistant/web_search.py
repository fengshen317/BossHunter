"""AI 助手的轻量联网检索与原文引用。"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx


class _SearchResultParser(HTMLParser):
	"""解析 DuckDuckGo HTML 搜索结果。"""

	def __init__(self) -> None:
		super().__init__()
		self.results: list[dict[str, str]] = []
		self._link_url = ""
		self._link_text: list[str] = []
		self._snippet_text: list[str] = []
		self._capture_link = False
		self._capture_snippet = False

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		attributes = dict(attrs)
		class_name = attributes.get("class", "") or ""
		if tag == "a" and "result__a" in class_name:
			self._capture_link = True
			self._link_url = _unwrap_search_url(attributes.get("href", "") or "")
			self._link_text = []
		elif tag in {"a", "div"} and "result__snippet" in class_name:
			self._capture_snippet = True
			self._snippet_text = []

	def handle_endtag(self, tag: str) -> None:
		if tag == "a" and self._capture_link:
			self._capture_link = False
		elif tag in {"a", "div"} and self._capture_snippet:
			self._capture_snippet = False
			if self._link_url:
				self.results.append(
					{
						"title": _clean_text(" ".join(self._link_text)),
						"url": self._link_url,
						"excerpt": _clean_text(" ".join(self._snippet_text)),
					}
				)
				self._link_url = ""

	def handle_data(self, data: str) -> None:
		if self._capture_link:
			self._link_text.append(data)
		if self._capture_snippet:
			self._snippet_text.append(data)


def _unwrap_search_url(url: str) -> str:
	"""还原搜索引擎跳转链接中的原始网页地址。"""
	parsed = urlparse(html.unescape(url))
	target = parse_qs(parsed.query).get("uddg", [""])[0]
	return unquote(target) if target else url


def _clean_text(value: str) -> str:
	"""压缩网页文本中的空白。"""
	return re.sub(r"\s+", " ", html.unescape(value)).strip()


def search_web(question: str, limit: int = 5) -> list[dict[str, str]]:
	"""检索网页并返回带原始 URL 的摘要。

	Args:
		question: 用户问题。
		limit: 最大来源数。

	Returns:
		标题、原始 URL 和搜索结果原文摘要。
	"""
	response = httpx.get(
		"https://html.duckduckgo.com/html/",
		params={"q": question},
		headers={"User-Agent": "Mozilla/5.0 BossHunter/2.0"},
		timeout=15,
		follow_redirects=True,
	)
	response.raise_for_status()
	parser = _SearchResultParser()
	parser.feed(response.text)
	return [item for item in parser.results if item["url"]][:limit]


__all__ = ["search_web"]
