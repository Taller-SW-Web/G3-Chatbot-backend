"""Product search normalization (SPEC-06): synonyms, fuzzy match, chips.

Pure: option lists come from the caller (the use case caches categories
and brands). Similarity uses the standard library; a score of 0.8 or
higher counts as a match.
"""
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

SIMILARITY_CUTOFF = 0.8
MAX_CHIPS = 6

# Peruvian Spanish aliases to canonical catalog names.
DEFAULT_SYNONYMS = {
    "zapas": "Zapatillas",
    "zapatilla": "Zapatillas",
    "chimpunes": "Zapatillas",
    "tabas": "Zapatillas",
    "remera": "Polos",
    "remeras": "Polos",
    "polo": "Polos",
    "mochila": "Mochilas y morrales",
    "morral": "Mochilas y morrales",
    "canguro": "Mochilas y morrales",
}


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(char for char in folded if not unicodedata.combining(char))


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _fold(left), _fold(right)).ratio()


@dataclass(frozen=True)
class NormalizedFilters:
    q: str | None = None
    category_id: str | None = None
    brand_id: str | None = None
    unresolved_category: str | None = None
    unresolved_brand: str | None = None
    chips: tuple[str, ...] = ()


class ProductNormalizer:
    def __init__(
        self,
        synonyms: dict[str, str] | None = None,
        categories: list[dict] | None = None,
        brands: list[dict] | None = None,
    ):
        self._synonyms = {
            _fold(key): value for key, value in (synonyms or DEFAULT_SYNONYMS).items()
        }
        self._categories = list(categories or [])
        self._brands = list(brands or [])

    def suggest(self, text: str, options: list[dict], key: str = "name") -> list[str]:
        """Close option names for a misspelled term, best first."""
        scored = [
            (_similarity(text, str(option.get(key, ""))), str(option.get(key, "")))
            for option in options
        ]
        return [
            name
            for score, name in sorted(scored, reverse=True)
            if score >= SIMILARITY_CUTOFF
        ]

    def normalize_category(self, text: str) -> dict | None:
        return self._match(text, self._categories)

    def normalize_brand(self, text: str) -> dict | None:
        return self._match(text, self._brands)

    def normalize_filters(
        self,
        q: str | None = None,
        categoria: str | None = None,
        marca: str | None = None,
    ) -> NormalizedFilters:
        category_id = unresolved_category = None
        if categoria:
            match = self.normalize_category(categoria)
            if match is None:
                unresolved_category = categoria
            else:
                category_id = match.get("category_id") or match.get("categoria_id")
        brand_id = unresolved_brand = None
        if marca:
            match = self.normalize_brand(marca)
            if match is None:
                unresolved_brand = marca
            else:
                brand_id = match.get("brand_id") or match.get("marca_id")
        chips = tuple(
            chip
            for chip in (
                f"q:{q}" if q else None,
                f"categoria:{categoria}" if categoria else None,
                f"marca:{marca}" if marca else None,
            )
            if chip is not None
        )[:MAX_CHIPS]
        return NormalizedFilters(
            q=q,
            category_id=category_id,
            brand_id=brand_id,
            unresolved_category=unresolved_category,
            unresolved_brand=unresolved_brand,
            chips=chips,
        )

    def _match(self, text: str, options: list[dict]) -> dict | None:
        folded = _fold(text)
        canonical = self._synonyms.get(folded)
        if canonical is not None:
            for option in options:
                if _fold(str(option.get("name", ""))) == _fold(canonical):
                    return option
        for option in options:
            if _fold(str(option.get("name", ""))) == folded:
                return option
        best: dict | None = None
        best_score = 0.0
        for option in options:
            score = _similarity(text, str(option.get("name", "")))
            if score > best_score:
                best, best_score = option, score
        return best if best_score >= SIMILARITY_CUTOFF else None
