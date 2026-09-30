"""Tagged data blocks for research prompts.

Every research prompt puts untrusted text (the Research Question, collected evidence, gaps, chat
messages, conversation memory) inside its own <tag>...</tag> blocks and tells the model that only
those delimiters are real. `neutralize` escapes '&', '<' and '>' in that text first, so it can
never contain a literal copy of a delimiter, like a fake "</research_question>" that closes the
block early and makes whatever follows look like trusted instructions. The escape is one-way: the
model reads the escaped text and nothing ever parses it back.
"""
from __future__ import annotations

import html
from collections.abc import Sequence


def neutralize(text: str) -> str:
    """Escapes '&', '<' and '>' in untrusted text before it goes inside a tagged block."""
    return html.escape(text, quote=False)


def text_block(tag: str, text: str) -> str:
    """One untrusted text value as a <tag>...</tag> block."""
    return f"<{tag}>\n{neutralize(text)}\n</{tag}>"


def bullet_block(tag: str, values: Sequence[str], *, max_items: int, max_item_len: int) -> str:
    """Untrusted short strings as a bounded "- value" list, or "(none)" when nothing is left.

    Blank values are dropped, the rest are stripped and cut to `max_item_len` characters, and
    only the first `max_items` of them are kept.
    """
    bounded = [v.strip()[:max_item_len] for v in values if v and v.strip()][:max_items]
    body = "\n".join(f"- {neutralize(v)}" for v in bounded) if bounded else "(none)"
    return f"<{tag}>\n{body}\n</{tag}>"
