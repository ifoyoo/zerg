"""HTML parser (selectolax / Lexbor)."""

from __future__ import annotations

from typing import Any

from selectolax.lexbor import LexborHTMLParser

# A page-wide CSS query walks the whole document (~1 µs per KB of markup),
# while a row query costs a couple of microseconds whatever the row holds.
# Both are measured (notes/candidates.md CAN-011), so batching rows needs
# roughly one row per 2.4 KB of markup to pay for itself.
_PAGE_QUERY_BYTES_PER_ROW = 2400


class Parser:
    """CSS helpers over a Lexbor tree."""

    __slots__ = ("_tree", "_size", "__text")

    def __init__(self, html: str):
        self._tree = LexborHTMLParser(html)
        self._size = len(html)
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
            self.__text = self._tree.body.text(strip=True) if self._tree.body else ""
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

        Page-wide queries replace per-row searching where that pays off; the
        result matches searching each row on its own — the first document-order
        descendant-or-self of each rule.
        """
        rows = self._tree.css(selector)
        if not rows:
            return []

        row_of = {row.mem_id: index for index, row in enumerate(rows)}
        get_row = row_of.get
        n_rows = len(rows)
        row_chain: dict[int, tuple[int, ...]] = {}

        def outer_rows(index: int) -> tuple[int, ...]:
            """Row indexes enclosing ``index`` — cached, usually empty."""
            chain = row_chain.get(index)
            if chain is None:
                found: list[int] = []
                parent = rows[index].parent
                while parent is not None:
                    found_row = get_row(parent.mem_id)
                    if found_row is not None:
                        found.append(found_row)
                    parent = parent.parent
                chain = row_chain[index] = tuple(found)
            return chain

        columns: list[tuple[str, list[Any], Any]] = []
        for key, spec in rules.items():
            if isinstance(spec, tuple):
                query, attr, default = spec[0], spec[1], None
            else:
                query, attr, default = spec, None, ""
            slots: list[Any] = [None] * n_rows
            # Plan per rule. Probing one row shows whether this query matches
            # about once per row — the shape a page-wide query is good at: then
            # each match is walked up to the rows it answers. When a row holds
            # many matches the per-row search wins instead, because it
            # short-circuits inside the row (~1.25 matches/row is the break-even).
            planned = False
            if (
                n_rows > 3
                and n_rows * _PAGE_QUERY_BYTES_PER_ROW >= self._size
                and len(rows[0].css(query)) < 2
            ):
                nodes = self._tree.css(query)
                if len(nodes) * 4 <= n_rows * 5:
                    for node in nodes:
                        value = (
                            node.attrs.get(attr)
                            if attr is not None
                            else node.text(strip=True)
                        )
                        index = get_row(node.mem_id)
                        if index is None:
                            parent: Any = node.parent
                            while parent is not None:
                                index = get_row(parent.mem_id)
                                if index is not None:
                                    break
                                parent = parent.parent
                            if index is None:
                                continue
                        if slots[index] is None:
                            slots[index] = value
                        # enclosing rows answer the same way (they are searched
                        # on their own, so the match is visible to them too)
                        for outer in outer_rows(index):
                            if slots[outer] is None:
                                slots[outer] = value
                    planned = True
            if not planned:
                for index, row in enumerate(rows):
                    node = row.css_first(query)
                    if node is None:
                        continue
                    slots[index] = (
                        node.attrs.get(attr)
                        if attr is not None
                        else node.text(strip=True)
                    )
            columns.append((key, slots, default))

        out: list[dict[str, Any]] = []
        for index in range(n_rows):
            item: dict[str, Any] = {}
            for key, slots, default in columns:
                value = slots[index]
                item[key] = default if value is None else value
            out.append(item)
        return out

    def __repr__(self) -> str:
        sample = self.text[:60] if self.text else ""
        return f"<Parser text={sample!r}...>"
