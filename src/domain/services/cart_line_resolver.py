"""Cart-line reference resolution (SPEC-11 Req. 2).

Resolves a line by `itemId`, by 1-based `itemRef` in listed order, or by
`nombre` substring. Zero or several matches report back instead of
guessing: the assistant asks "¿Cuáles?" with the options. Pure.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class LineResolution:
    item_id: str | None = None
    ambiguous: tuple[dict, ...] = ()

    @property
    def found(self) -> bool:
        return self.item_id is not None


def _fold(text: str) -> str:
    return text.strip().lower()


class CartLineResolver:
    def resolve(
        self,
        lines: list[dict],
        *,
        item_id: str | None = None,
        item_ref: int | None = None,
        name: str | None = None,
    ) -> LineResolution:
        if item_id is not None:
            for line in lines:
                if str(line.get("item_id")) == item_id:
                    return LineResolution(item_id=str(line["item_id"]))
            return LineResolution()
        if item_ref is not None:
            if 1 <= item_ref <= len(lines):
                return LineResolution(item_id=str(lines[item_ref - 1]["item_id"]))
            return LineResolution()
        if name is not None:
            matches = [
                line for line in lines if _fold(name) in _fold(str(line.get("name", "")))
            ]
            if len(matches) == 1:
                return LineResolution(item_id=str(matches[0]["item_id"]))
            return LineResolution(ambiguous=tuple(matches))
        return LineResolution()
