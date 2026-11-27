"""Product search normalization (SPEC-06): synonyms, fuzzy match, chips.

Pure: option lists come from the caller (the use case caches categories
and brands). Similarity uses the standard library; a score of 0.8 or
higher counts as a match.
"""
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

SIMILARITY_CUTOFF = 0.8
MAX_CHIPS = 6
# Connectors the LLM adds around the real search terms ("zapatillas de running").
# Productos matches literal substrings, so sending them empties the result set.
SPANISH_STOPWORDS = frozenset({"de", "para", "con", "el", "la", "en", "y"})

# Peruvian Spanish aliases to canonical catalog names, in preference order.
# G6 seeded taxonomy uses Calzado (Fútbol, Running), Ropa and Equipamiento y
# Accesorios; the simplified PRODUCTS_MODE=fake catalog uses Zapatillas, Polos
# and Mochilas y morrales. The first name present in the current catalog wins,
# so the same dictionary works against both without inventing categories.
DEFAULT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "zapas": ("Calzado", "Zapatillas"),
    "zapatilla": ("Calzado", "Zapatillas"),
    "zapatillas": ("Calzado", "Zapatillas"),
    "chimpun": ("Fútbol", "Zapatillas"),
    "chimpunes": ("Fútbol", "Zapatillas"),
    "tabas": ("Fútbol", "Zapatillas"),
    "tacho": ("Fútbol", "Zapatillas"),
    "tachos": ("Fútbol", "Zapatillas"),
    "fulbito": ("Fútbol", "Zapatillas"),
    "zapatillas de fulbito": ("Fútbol", "Zapatillas"),
    "polo": ("Ropa", "Polos"),
    "polos": ("Ropa", "Polos"),
    "remera": ("Ropa", "Polos"),
    "remeras": ("Ropa", "Polos"),
    "camiseta": ("Ropa", "Polos"),
    "camisetas": ("Ropa", "Polos"),
    "buzo": ("Ropa", "Polos"),
    "buzos": ("Ropa", "Polos"),
    "mochila": ("Equipamiento y Accesorios", "Mochilas y morrales"),
    "mochilas": ("Equipamiento y Accesorios", "Mochilas y morrales"),
    "morral": ("Equipamiento y Accesorios", "Mochilas y morrales"),
    "morrales": ("Equipamiento y Accesorios", "Mochilas y morrales"),
    "canguro": ("Equipamiento y Accesorios", "Mochilas y morrales"),
    "canguros": ("Equipamiento y Accesorios", "Mochilas y morrales"),
}

# G6 searches literal substrings over Spanish product descriptions, which use
# "running" instead of "correr". These are search-vocabulary aliases, not
# category synonyms: they translate the term but never resolve a category.
SEARCH_ALIASES = {
    "correr": "running",
    "trotar": "running",
}


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(char for char in folded if not unicodedata.combining(char))


def _key(text: str) -> str:
    """Fold and drop connectors so phrases match with or without "de"/"y"."""
    return " ".join(
        token for token in _fold(text).split() if token not in SPANISH_STOPWORDS
    )


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
        synonyms: Mapping[str, str | Sequence[str]] | None = None,
        categories: list[dict] | None = None,
        brands: list[dict] | None = None,
    ):
        source = DEFAULT_SYNONYMS if synonyms is None else synonyms
        self._synonyms = {
            _key(key): self._canonical_names(value) for key, value in source.items()
        }
        self._categories = list(categories or [])
        self._brands = list(brands or [])
        # Multi-word aliases and category names must be tested before their
        # individual tokens ("zapatillas de fulbito" is not generic footwear).
        self._max_phrase_words = max(
            (len(alias.split()) for alias in self._synonyms), default=1
        )
        self._max_phrase_words = max(
            self._max_phrase_words,
            max((len(_key(str(option.get("name", ""))).split())
                 for option in self._categories), default=1),
        )

    @staticmethod
    def _canonical_names(value: str | Sequence[str]) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value,)
        return tuple(str(name) for name in value)

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
        normalized_q, q_category_id = self._normalize_query(q)
        category_id = unresolved_category = None
        if categoria:
            match = self.normalize_category(categoria)
            if match is None:
                unresolved_category = categoria
            else:
                category_id = match.get("category_id") or match.get("categoria_id")
        elif q_category_id is not None:
            # A category named inside the free text is a structured filter,
            # not part of the literal substring Productos receives as `q`.
            category_id = q_category_id
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
            q=normalized_q,
            category_id=category_id,
            brand_id=brand_id,
            unresolved_category=unresolved_category,
            unresolved_brand=unresolved_brand,
            chips=chips,
        )

    def _normalize_query(self, q: str | None) -> tuple[str | None, str | None]:
        """Fold, collapse whitespace, drop connectors and translate search aliases.

        Multi-word aliases and category names are tested before individual
        tokens, so "zapatillas de fulbito" resolves as football footwear rather
        than as generic shoes. The first token(s) that name a known category
        (directly or through a synonym) become `category_id`; the remaining
        tokens stay as the literal `q` sent to Productos.
        """
        if not q:
            return None, None
        tokens = [
            SEARCH_ALIASES.get(token, token)
            for token in _fold(q).split()
            if token not in SPANISH_STOPWORDS
        ]
        category_id = None
        remaining: list[str] = []
        index = 0
        while index < len(tokens):
            match = None
            consumed = 1
            longest = min(self._max_phrase_words, len(tokens) - index)
            for size in range(longest, 1, -1):
                phrase = " ".join(tokens[index:index + size])
                match = self._match(phrase, self._categories, fuzzy=False)
                if match is not None:
                    consumed = size
                    break
            if match is None:
                match = self._match(tokens[index], self._categories)
            if match is not None:
                matched_id = match.get("category_id") or match.get("categoria_id")
                if category_id is None or category_id == matched_id:
                    category_id = matched_id
                    index += consumed
                    continue
            remaining.append(tokens[index])
            index += 1
        return " ".join(remaining) or None, category_id

    def _match(self, text: str, options: list[dict], *, fuzzy: bool = True) -> dict | None:
        """Exact name, then synonym candidates, then (optionally) similarity.

        A synonym holds the real G6 name first and the fake-catalog name as a
        fallback; a candidate that is not in the current catalog is skipped.
        """
        folded = _key(text)
        for option in options:
            if _key(str(option.get("name", ""))) == folded:
                return option
        for canonical in self._synonyms.get(folded, ()):
            for option in options:
                if _key(str(option.get("name", ""))) == _key(canonical):
                    return option
        if not fuzzy:
            return None
        best: dict | None = None
        best_score = 0.0
        for option in options:
            score = _similarity(folded, _key(str(option.get("name", ""))))
            if score > best_score:
                best, best_score = option, score
        return best if best_score >= SIMILARITY_CUTOFF else None
