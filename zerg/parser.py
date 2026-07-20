"""HTML parser (selectolax / Lexbor)."""

from __future__ import annotations

from typing import Any

from selectolax.lexbor import LexborHTMLParser


class Parser:
    """CSS helpers over a Lexbor tree."""

    __slots__ = ("_tree", "_html", "__text")

    def __init__(self, html: str):
        self._html = html
        self._tree = LexborHTMLParser(html)
        self.__text: str | None = None

    @classmethod
    def from_response(cls, response: Any) -> Parser:
        """Build from an object that has ``.text``."""
        return cls(response.text)

    def css(self, selector: str, default: str | None = None) -> str:
        """Text of the first match."""
        node = self._tree.css_first(selector)
        return node.text(strip=True) if node else (default or "")

    def css_all(self, selector: str) -> list[str]:
        """Text of all matches."""
        return [n.text(strip=True) for n in self._tree.css(selector)]

    def css_first(self, selector: str):
        """First matching node."""
        return self._tree.css_first(selector)

    def css_attr(
        self, selector: str, attr: str, default: str | None = None
    ) -> str | None:
        """Attribute of the first match."""
        node = self._tree.css_first(selector)
        if node is None:
            return default
        return node.attrs.get(attr, default)

    def css_attrs(self, selector: str, attr: str) -> list[str | None]:
        """Attribute of all matches."""
        return [n.attrs.get(attr) for n in self._tree.css(selector)]

    @property
    def text(self) -> str:
        """Visible text (lazy)."""
        if self.__text is None:
            self.__text = (
                self._tree.body.text(strip=True) if self._tree.body else ""
            )
        return self.__text

    def text_lines(self) -> list[str]:
        """Non-empty text lines."""
        return [line for line in self.text.split("\n") if line.strip()]

    def extract(self, rules: dict[str, str]) -> dict[str, str]:
        """Extract ``{key: css_selector}`` fields."""
        return {key: self.css(sel) for key, sel in rules.items()}

    def extract_all(
        self, selector: str, rules: dict[str, str | tuple[str, str]]
    ) -> list[dict[str, Any]]:
        """Extract fields under each ``selector`` match.

        Rule value: ``str`` for text, ``(sel, attr)`` for attribute.
        """
        results: list[dict[str, Any]] = []
        for node in self._tree.css(selector):
            sub = _SubParser(node)
            item: dict[str, Any] = {}
            for key, spec in rules.items():
                if isinstance(spec, tuple):
                    item[key] = sub.css_attr(*spec)
                else:
                    item[key] = sub.css(spec)
            results.append(item)
        return results

    def __repr__(self) -> str:
        sample = self.text[:60] if self.text else ""
        return f"<Parser text={sample!r}...>"


class _SubParser:
    """Parser scoped to one node."""

    __slots__ = ("_node",)

    def __init__(self, node: Any):
        self._node = node

    def css(self, selector: str) -> str:
        n = self._node.css_first(selector)
        return n.text(strip=True) if n else ""

    def css_attr(
        self, selector: str, attr: str, default: str | None = None
    ) -> str | None:
        n = self._node.css_first(selector)
        return n.attrs.get(attr, default) if n else default
