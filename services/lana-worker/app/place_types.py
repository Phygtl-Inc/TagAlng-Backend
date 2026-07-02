"""Google Places API (New) food/dining `includedType` enum whitelist + attribute allowlist.

The tip-seek personalizer lets an LLM pick a Google place TYPE (e.g. italian_restaurant)
and per-place boolean ATTRIBUTES (e.g. servesVegetarianFood) to filter a recommendation by
the user's identity claims. Both are validated against these sets before they reach the
Places request: an unknown `includedType` returns INVALID_ARGUMENT from Google (breaks the
whole call), and an unknown attribute field in the FieldMask does the same — so we drop
anything not listed here rather than trust the model's spelling.

Source: developers.google.com/maps/documentation/places/web-service/place-types (Table A),
food & drink subset. Keep in sync if Google adds cuisines.
"""

from __future__ import annotations

# Food/dining types the personalizer may use as `includedType` (Table A, food & drink).
FOOD_PLACE_TYPES: frozenset[str] = frozenset({
    # cuisine-specific restaurants
    "acai_shop", "afghani_restaurant", "african_restaurant", "american_restaurant",
    "argentinian_restaurant", "asian_fusion_restaurant", "asian_restaurant",
    "australian_restaurant", "austrian_restaurant", "bangladeshi_restaurant",
    "basque_restaurant", "bavarian_restaurant", "belgian_restaurant", "brazilian_restaurant",
    "british_restaurant", "burmese_restaurant", "californian_restaurant", "cambodian_restaurant",
    "cantonese_restaurant", "caribbean_restaurant", "chilean_restaurant",
    "chinese_noodle_restaurant", "chinese_restaurant", "colombian_restaurant",
    "croatian_restaurant", "cuban_restaurant", "czech_restaurant", "danish_restaurant",
    "dim_sum_restaurant", "dutch_restaurant", "eastern_european_restaurant",
    "ethiopian_restaurant", "european_restaurant", "falafel_restaurant", "filipino_restaurant",
    "fish_and_chips_restaurant", "fondue_restaurant", "french_restaurant", "fusion_restaurant",
    "german_restaurant", "greek_restaurant", "gyro_restaurant", "hawaiian_restaurant",
    "hungarian_restaurant", "indian_restaurant", "indonesian_restaurant", "irish_pub",
    "irish_restaurant", "israeli_restaurant", "italian_restaurant", "japanese_curry_restaurant",
    "japanese_izakaya_restaurant", "japanese_restaurant", "korean_barbecue_restaurant",
    "korean_restaurant", "latin_american_restaurant", "lebanese_restaurant",
    "malaysian_restaurant", "mediterranean_restaurant", "mexican_restaurant",
    "middle_eastern_restaurant", "mongolian_barbecue_restaurant", "moroccan_restaurant",
    "north_indian_restaurant", "oyster_bar_restaurant", "pakistani_restaurant",
    "persian_restaurant", "peruvian_restaurant", "polish_restaurant", "portuguese_restaurant",
    "ramen_restaurant", "romanian_restaurant", "russian_restaurant", "scandinavian_restaurant",
    "seafood_restaurant", "shawarma_restaurant", "soul_food_restaurant", "soup_restaurant",
    "south_american_restaurant", "south_indian_restaurant", "southwestern_us_restaurant",
    "spanish_restaurant", "sri_lankan_restaurant", "steak_house", "sushi_restaurant",
    "swiss_restaurant", "taiwanese_restaurant", "thai_restaurant", "tibetan_restaurant",
    "tonkatsu_restaurant", "turkish_restaurant", "ukrainian_restaurant", "vegan_restaurant",
    "vegetarian_restaurant", "vietnamese_restaurant", "western_restaurant", "yakiniku_restaurant",
    "yakitori_restaurant",
    # general food & drink
    "bagel_shop", "bakery", "bar", "bar_and_grill", "barbecue_restaurant", "beer_garden",
    "brewery", "brewpub", "brunch_restaurant", "buffet_restaurant", "burrito_restaurant",
    "cafe", "cafeteria", "cajun_restaurant", "cake_shop", "candy_store", "cat_cafe",
    "chicken_restaurant", "chicken_wings_restaurant", "chocolate_factory", "chocolate_shop",
    "cocktail_bar", "coffee_roastery", "coffee_shop", "coffee_stand", "confectionery", "deli",
    "dessert_restaurant", "dessert_shop", "diner", "dog_cafe", "donut_shop", "dumpling_restaurant",
    "fast_food_restaurant", "fine_dining_restaurant", "food_court", "family_restaurant",
    "gastropub", "halal_restaurant", "hamburger_restaurant", "hookah_bar", "hot_dog_restaurant",
    "hot_dog_stand", "hot_pot_restaurant", "ice_cream_shop", "juice_shop", "kebab_shop",
    "lounge_bar", "meal_delivery", "meal_takeaway", "noodle_shop", "pastry_shop",
    "pizza_delivery", "pizza_restaurant", "pub", "restaurant", "salad_shop", "sandwich_shop",
    "snack_bar", "sports_bar", "taco_restaurant", "tea_house", "tex_mex_restaurant",
    "wine_bar", "winery",
})

# Per-place boolean attribute fields the personalizer may require + we can verify. These map
# 1:1 to Places API (New) fields; requesting them puts the call in the pricier
# "Enterprise + Atmosphere" billing tier, so we only request the ones a filter actually needs.
VERIFIABLE_ATTRS: frozenset[str] = frozenset({
    "goodForChildren", "menuForChildren", "servesVegetarianFood",
    "servesBreakfast", "servesLunch", "servesDinner", "servesBrunch",
    "servesCoffee", "servesDessert", "dineIn", "takeout", "delivery",
    "outdoorSeating", "reservable", "goodForGroups", "allowsDogs",
})


def valid_included_type(t: str | None) -> str | None:
    """Return `t` if it's a known food place type, else None (so a bad enum is dropped,
    not sent — an unknown includedType errors the whole Places request)."""
    s = str(t or "").strip().lower()
    return s if s in FOOD_PLACE_TYPES else None


def valid_attrs(attrs: list[str] | None) -> list[str]:
    """Filter a requested attribute list down to fields we can actually request+verify."""
    out: list[str] = []
    for a in attrs or []:
        s = str(a or "").strip()
        if s in VERIFIABLE_ATTRS and s not in out:
            out.append(s)
    return out
