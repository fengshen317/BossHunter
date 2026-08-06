"""HTML resume templates used by the PDF renderer."""

from __future__ import annotations

import base64
import re
from pathlib import Path

import markdown2

DEFAULT_TEMPLATE = "modern"
DEFAULT_THEME_COLOR = "#0F766E"
SUPPORTED_TEMPLATES = {"modern", "executive", "ats"}
MAX_AVATAR_BYTES = 5 * 1024 * 1024

_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

_BASE_CSS = """
:root {
    --accent: #0F766E;
    --ink: #17202A;
    --muted: #5D6772;
    --paper: #FFFFFF;
    --hairline: #DCE3E1;
}
* { box-sizing: border-box; }
@page { size: A4; margin: 0; }
html, body { margin: 0; padding: 0; background: #EEF1EF; }
body {
    color: var(--ink);
    font-family: "Source Han Sans SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
    font-size: 10.2pt;
    line-height: 1.55;
}
.resume-shell {
    position: relative;
    width: 210mm;
    min-height: 297mm;
    margin: 0 auto;
    background: var(--paper);
}
.resume-content h1 {
    margin: 0;
    font-family: "Source Han Serif SC", "Noto Serif CJK SC", "STSong", serif;
    font-size: 25pt;
    line-height: 1.18;
    letter-spacing: .08em;
}
.resume-content h2 {
    margin: 16px 0 8px;
    color: var(--accent);
    font-size: 11.5pt;
    line-height: 1.3;
    letter-spacing: .08em;
    break-after: avoid;
}
.resume-content h3 {
    margin: 10px 0 3px;
    font-size: 10.5pt;
    break-after: avoid;
}
.resume-content p { margin: 4px 0; }
.resume-content ul { margin: 5px 0 8px; padding-left: 18px; }
.resume-content li { margin: 2px 0; }
.resume-content strong { font-weight: 700; }
.resume-content a { color: inherit; text-decoration: none; }
.resume-content table { width: 100%; border-collapse: collapse; margin: 8px 0; }
.resume-content th, .resume-content td { padding: 5px 7px; border: 1px solid var(--hairline); text-align: left; }
.resume-content h2, .resume-content h3, .resume-content li, .resume-content table { break-inside: avoid; }
.resume-avatar {
    position: absolute;
    z-index: 2;
    width: 28mm;
    height: 35mm;
    object-fit: cover;
    object-position: center 24%;
    background: #F4F6F5;
}
@media print {
    html, body { background: #FFFFFF; }
    .resume-shell { margin: 0; }
}
"""

_MODERN_CSS = """
.resume-shell--modern {
    padding: 18mm 19mm 16mm;
    border-top: 7mm solid var(--accent);
}
.resume-shell--modern::before {
    content: "";
    position: absolute;
    top: 13mm;
    right: 13mm;
    width: 42mm;
    height: 42mm;
    border: 1px solid color-mix(in srgb, var(--accent) 24%, white);
    border-radius: 50%;
}
.resume-shell--modern .resume-content > h1:first-child {
    max-width: 145mm;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--hairline);
}
.resume-shell--modern .resume-content > h2 {
    display: flex;
    align-items: center;
    gap: 8px;
}
.resume-shell--modern .resume-content > h2::after {
    content: "";
    height: 1px;
    flex: 1;
    background: linear-gradient(90deg, var(--hairline), transparent);
}
.resume-shell--modern .resume-avatar {
    top: 16mm;
    right: 19mm;
    border: 3px solid white;
    border-radius: 3mm;
    box-shadow: 0 7px 20px rgba(20, 40, 36, .18);
}
.resume-shell--modern:has(.resume-avatar) .resume-content > h1:first-child {
    max-width: 125mm;
    min-height: 35mm;
}
"""

_EXECUTIVE_CSS = """
.resume-shell--executive {
    padding: 20mm 20mm 17mm;
    border-left: 8mm solid var(--accent);
}
.resume-shell--executive .resume-content > h1:first-child {
    margin-left: -4px;
    padding: 0 35mm 10px 0;
    border-bottom: 2px solid var(--ink);
}
.resume-shell--executive .resume-content > h2 {
    color: var(--ink);
    padding: 4px 0 5px 10px;
    border-left: 3px solid var(--accent);
    border-bottom: 1px solid var(--hairline);
}
.resume-shell--executive .resume-avatar {
    top: 17mm;
    right: 20mm;
    border: 1px solid var(--hairline);
    border-radius: 50%;
}
"""

_ATS_CSS = """
.resume-shell--ats { padding: 16mm 18mm; }
.resume-shell--ats .resume-content h1 {
    font-family: "Microsoft YaHei", sans-serif;
    font-size: 21pt;
    letter-spacing: 0;
    padding-bottom: 6px;
    border-bottom: 2px solid #222;
}
.resume-shell--ats .resume-content h2 {
    color: #111;
    font-size: 11.5pt;
    letter-spacing: 0;
    padding-bottom: 3px;
    border-bottom: 1px solid #777;
}
.resume-shell--ats .resume-content a { text-decoration: underline; }
"""


def build_resume_html(
    markdown_text: str,
    template_name: str = DEFAULT_TEMPLATE,
    avatar_path: Path | str | None = None,
    show_avatar: bool = True,
    theme_color: str = DEFAULT_THEME_COLOR,
) -> str:
    """Render resume Markdown into a self-contained printable HTML document.

    Args:
        markdown_text: Validated resume Markdown.
        template_name: One of ``modern``, ``executive``, or ``ats``.
        avatar_path: Optional local JPEG, PNG, or WebP portrait.
        show_avatar: Whether visual templates should include the portrait.
        theme_color: Six-digit hexadecimal accent color.

    Returns:
        A complete UTF-8 HTML document with embedded CSS and optional avatar.
    """
    selected_template = template_name if template_name in SUPPORTED_TEMPLATES else DEFAULT_TEMPLATE
    selected_color = theme_color if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(theme_color)) else DEFAULT_THEME_COLOR
    html_body = markdown2.markdown(markdown_text, extras=["tables", "fenced-code-blocks"])
    avatar_data_uri = _avatar_data_uri(avatar_path) if show_avatar and selected_template != "ats" else None
    avatar_html = (
        f'<img class="resume-avatar" src="{avatar_data_uri}" alt="个人头像">'
        if avatar_data_uri
        else ""
    )
    template_css = {
        "modern": _MODERN_CSS,
        "executive": _EXECUTIVE_CSS,
        "ats": _ATS_CSS,
    }[selected_template]

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
{_BASE_CSS}
:root {{ --accent: {selected_color}; }}
{template_css}
</style>
</head>
<body>
<main class="resume-shell resume-shell--{selected_template}">
{avatar_html}
<article class="resume-content">
{html_body}
</article>
</main>
</body>
</html>"""


def _avatar_data_uri(avatar_path: Path | str | None) -> str | None:
    """Return a safe embedded image URI for a supported local portrait."""
    if not avatar_path:
        return None
    path = Path(avatar_path)
    mime_type = _MIME_TYPES.get(path.suffix.lower())
    if not mime_type or not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data or len(data) > MAX_AVATAR_BYTES:
        return None
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"
