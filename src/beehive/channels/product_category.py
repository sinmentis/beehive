"""One shared product category for store listings, whatever the storefront calls them.

Every store names its product types its own way: "Clothing>Tops>Short Sleeved T-Shirts",
"Tops - S/S,S/S Pullovers", "T-Shirts", or nothing at all. `classify_product` maps a listing onto
one small fixed set of categories so a Monitor Channel can offer a single type filter across all
of its Sources.

The store's own product type is trusted first and the title is only a fallback, because a title
names things the product merely relates to ("Boot Bag", "Tent Footprint"). The category is
derived when a page is built and is never stored, so a rule change applies to every listing at
once without a refetch or a migration.
"""
from __future__ import annotations

import re
from enum import Enum


class ProductCategory(str, Enum):
    TOPS = "tops"
    MIDLAYERS = "midlayers"
    JACKETS = "jackets"
    PANTS = "pants"
    SHORTS = "shorts"
    DRESSES = "dresses"
    UNDERWEAR = "underwear"
    FOOTWEAR = "footwear"
    SOCKS = "socks"
    ACCESSORIES = "accessories"
    BAGS = "bags"
    OTHER = "other"


def _rule(category: ProductCategory, pattern: str) -> tuple[ProductCategory, re.Pattern[str]]:
    return category, re.compile(pattern, re.IGNORECASE)


# First match wins, so the order carries the meaning: the noun a compound name ends on has to be
# tested before the nouns that only describe it. "Boot Bag" is a bag, "Shoe Socks" are socks,
# "Shell Mitts" are accessories, "Fleece Pants" and "Softshell Pants" are pants, and a
# "Short Sleeved T-Shirt" must never reach the shorts rule.
_RULES = (
    # Equipment whose name contains a garment or bag word.
    _rule(
        ProductCategory.OTHER,
        r"\b(?:sleeping bags?|bag liners?|bivv?y bags?|tents?|helmets?|snowboards?|"
        r"bindings?|poles?|stoves?|bottles?|insoles?|laces|wetsuits?|"
        r"pack covers?|rain ?covers?|shoe care|waterproofing|gift cards?|dogs?)\b",
    ),
    _rule(
        ProductCategory.BAGS,
        r"\b(?:bags?|backpacks?|daypacks?|rucksacks?|duffels?|duffles?|totes?|luggage|"
        r"suitcases?|bumbags?|satchels?|clutch(?:es)?|"
        r"(?:hip|waist|bum|day|hydration|running|race|travel|hiking|tramping) ?(?:packs?|belts?|vests?)|"
        r"packs & storage)\b|(?<!\d )(?<!\d-)\bpacks?$",
    ),
    _rule(ProductCategory.SOCKS, r"\bsocks?\b"),
    _rule(
        ProductCategory.ACCESSORIES,
        r"\b(?:gloves?|mitts?|mittens?|handwear|hats?|caps?|beanies?|headbands?|headwear|balaclavas?|"
        r"visors?|buffs?|neck ?(?:gaiters?|warmers?|tubes?)|neckwarmers?|scarf|scarves|belts?|"
        r"sunglasses|eyewear|goggles?|gaiters?|wallets?|watch(?:es)?|jewell?ery|"
        r"arm ?(?:warmers?|sleeves?))\b",
    ),
    _rule(
        ProductCategory.FOOTWEAR,
        r"\b(?:shoes?|sneakers?|boots?|booties|sandals?|footwear|trainers|slides|jandals?|"
        r"cleats|spikes|clogs?|loafers?|pumps|heels|mules|flats|espadrilles|slippers?|"
        r"flip[- ]flops?)\b",
    ),
    _rule(
        ProductCategory.UNDERWEAR,
        r"\b(?:bras?|bralettes?|briefs?|boxers?|underwear|undies|trunks?|thongs?|hipkinis?|"
        r"hipsters?|lingerie|bikinis?|swimwear|swimsuits?|swim|beachwear|rash ?(?:guards?|vests?|ies)|"
        r"rashies)\b",
    ),
    _rule(
        ProductCategory.DRESSES,
        r"\b(?:(?:mini|midi|maxi|shirt|sun)?dress(?:es)?|(?:mini|midi|maxi)?skirts?|skorts?|gowns?|"
        r"jumpsuits?|rompers?|playsuits?)\b",
    ),
    _rule(
        ProductCategory.SHORTS,
        r"\b(?:boardshorts?|shorts)\b(?![- ]?sleeve)|"
        r"\bshort\b(?![- ]?(?:sleeve|tee|legging|tight|jacket|coat|dress|skirt|boot|"
        r"dated|leash|inseam|leg|length|long))",
    ),
    _rule(
        ProductCategory.PANTS,
        r"\b(?:pants?|trousers?|tights?|leggings?|joggers?|jeans|bibs?|overtrousers|"
        r"sweatpants|trackpants|chinos?|salopettes?|bottoms?)\b",
    ),
    # A sun hoody is a shirt with a hood, not a warm layer.
    _rule(ProductCategory.TOPS, r"\bsun (?:hood(?:y|ie)|shirt)\b"),
    _rule(
        ProductCategory.JACKETS,
        r"\b(?:jackets?|coats?|parkas?|vests?|gilets?|shells?|windbreakers?|anoraks?|blazers?|"
        r"puffers?|ponchos?|raincoats?|rainwear|outerwear|windshirts?|track tops?)\b",
    ),
    _rule(
        ProductCategory.MIDLAYERS,
        r"\b(?:hood(?:y|ie|ies)|sweatshirts?|sweats?|sweaters?|fleeces?|jumpers?|knitwear|"
        r"cardigans?|midlayers?)\b",
    ),
    _rule(
        ProductCategory.TOPS,
        r"\b(?:tees?|t-?shirts?|shirts?|tops?|polos?|singlets?|tanks?|jerseys?|crewe?s?|"
        r"blouses?|camis?|camisoles?|bodysuits?|"
        r"(?:long|short)[- ]?sleeved?|l/s|s/s|ls|ss)\b",
    ),
)

# A product type under one of these departments is equipment, where only a bag or footwear type
# counts ("Gear>Packs & Storage>Backpacks", "Gear>Climbing>Climbing Shoes"). Everything else there
# is OTHER and the title is not consulted: a "Tent Footprint", a "Boot Dryer" and a bottle's
# "Hot Cap" are not footwear or headwear.
_EQUIPMENT_TYPE = re.compile(
    r"^\s*(?:gear|food|nutrition|equipment|electronics|hardware)\b", re.IGNORECASE
)
_EQUIPMENT_CATEGORIES = frozenset({ProductCategory.BAGS, ProductCategory.FOOTWEAR})
# What a title may refine a store's broad "Tops" type into: Patagonia files hoodies, and Stirling
# Sports files track tops, under plain tops.
_TOPS_REFINEMENTS = frozenset({ProductCategory.MIDLAYERS, ProductCategory.JACKETS})
# Some stores file base layers under a bare "Underwear" department. There the title decides
# whether the listing is a top or a pair of pants; briefs and bras stay underwear.
_UNDERWEAR_DEPARTMENT = re.compile(r"\bunderwear\b", re.IGNORECASE)
_UNDERWEAR_REFINEMENTS = frozenset({ProductCategory.TOPS, ProductCategory.PANTS})
# Base layers come as tops and bottoms, so the word alone only decides when nothing else does:
# "Base Layer Bottoms" are pants, a bare "Baselayer" set is filed with tops.
_BASE_LAYER = re.compile(r"\b(?:base ?layers?|thermals?|next to skin)\b", re.IGNORECASE)


def _match(text: str) -> ProductCategory | None:
    for category, pattern in _RULES:
        if pattern.search(text):
            return category
    return None


def classify_product(product_type: str | None, title: str | None) -> ProductCategory:
    """The shared category for a listing; OTHER when nothing names one."""
    product_type = (product_type or "").strip()
    title = (title or "").strip()
    from_type = _match(product_type) if product_type else None
    if product_type and _EQUIPMENT_TYPE.search(product_type):
        return from_type if from_type in _EQUIPMENT_CATEGORIES else ProductCategory.OTHER
    from_title = _match(title) if title else None
    if from_type is ProductCategory.TOPS:
        return from_title if from_title in _TOPS_REFINEMENTS else from_type
    if from_type is ProductCategory.UNDERWEAR and _UNDERWEAR_DEPARTMENT.search(product_type):
        return from_title if from_title in _UNDERWEAR_REFINEMENTS else from_type
    if from_type is not None:
        return from_type
    if from_title is not None:
        return from_title
    if _BASE_LAYER.search(product_type) or _BASE_LAYER.search(title):
        return ProductCategory.TOPS
    return ProductCategory.OTHER
