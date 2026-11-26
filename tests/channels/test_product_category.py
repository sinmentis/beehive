"""The shared product category: real product types and titles from the monitored stores."""
import pytest

from beehive.channels.product_category import ProductCategory, classify_product

C = ProductCategory


@pytest.mark.parametrize(
    ("product_type", "title", "expected"),
    [
        # Each store spells the same thing its own way.
        ("Clothing>Tops>Short Sleeved T-Shirts", "Merino 150 Tech Lite Men's SS Tee", C.TOPS),
        ("Tops - S/S,S/S Pullovers", "Men's P-6 Logo Responsibili-Tee", C.TOPS),
        ("T-Shirts", "Adizero Essentials Running Tee", C.TOPS),
        ("Short Sleeve T-Shirt", "SHAKEout CORE", C.TOPS),
        ("Mens Tee Shirts", "Montane Clearance Dart T-Shirt - Mens", C.TOPS),
        ("Licenced", "Real Madrid 25/26 Home Jersey", C.TOPS),
        ("Clothing>Bottoms>Shorts", "Paramount Men's Shorts", C.SHORTS),
        ("Men's Legwear", "Montane Clearance Jetstream 7\" Short - Mens", C.SHORTS),
        ("Bike Short Liners and Bibs", "Mons Royale Men's Epic Merino Bike Liner", C.SHORTS),
        ("Clothing>Bottoms>Pants", "Ferrosi Women's Pants - Short Inseam", C.PANTS),
        ("Clothing>Bottoms>Leggings", "Merino 260 Tech Men's Leggings", C.PANTS),
        ("Men's Legwear", "Arc'teryx Clearance Gamma Pant Regular - Mens", C.PANTS),
        ("Base Layer Bottoms", "Patagonia Men's Capilene Air Baselayer Bottoms", C.PANTS),
        ("Clothing>Jackets and Vests>Jackets", "Coldfront Women's Down Hoodie", C.JACKETS),
        ("Shell Jackets-Men", "Beta SL Jacket Men's", C.JACKETS),
        ("Insulated Jackets", "Scott Women's Ultimate Warm Ski Jacket", C.JACKETS),
        ("Clothing>Tops>Sweatshirts", "Freewheel Women's Half Zip Hoodie", C.MIDLAYERS),
        ("Fleece", "DAYBREAKER 1/2 ZIP FLEECE, Black", C.MIDLAYERS),
        ("Footwear>Shoes", "Olympus 275 HiLo Men's Trail Running Shoe", C.FOOTWEAR),
        ("Sneakers", "Killshot 2 Leather Mens", C.FOOTWEAR),
        ("Alpine Ski Boots", "Rossignol Speed 120 HV GW 26 Ski Boots", C.FOOTWEAR),
        ("Gear>Climbing>Climbing Shoes", "Momentum Men's Lace Climbing Shoes", C.FOOTWEAR),
        ("Clothing>Accessories>Socks", "Hike+ Light Crew Socks", C.SOCKS),
        ("Clothing>Accessories>Headwear", "Merino 200 Oasis Reversible Beanie", C.ACCESSORIES),
        ("Ski Gloves", "Scott Women's Ultimate Warm Ski Glove", C.ACCESSORIES),
        ("Non-Apparel,Hats", "P-6 Logo LoPro Trucker Hat", C.ACCESSORIES),
        ("Gear>Packs & Storage>Backpacks", "Speed 40 Climbing Pack", C.BAGS),
        ("Running Vest With Flasks", "ADV SKIN 5", C.BAGS),
        ("Clothing>Tops>Sports Bras", "Sierra Women's Merino Sports Bra", C.UNDERWEAR),
        ("Clothing>Underwear>Lower Body", "Merino 150 Anatomica Men's Boxers", C.UNDERWEAR),
        ("Mini", "Crystal-embellished minidress", C.DRESSES),
        ("Cocktail", "Denim miniskirt", C.DRESSES),
    ],
)
def test_store_product_types_map_onto_shared_categories(product_type, title, expected):
    assert classify_product(product_type, title) is expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Land & Sea listings carry no product type at all, so the title decides.
        ("Ballast Short Sleeved Shirt - Black", C.TOPS),
        ("Angler Tech Cargo Shorts - Olive", C.SHORTS),
        ("Bobs Stewart Trackpants", C.PANTS),
        ("Women's Houdini Jacket", C.JACKETS),
        ("Men's Box NSE Pullover Hoodie - TNF Black", C.MIDLAYERS),
        ("Boot Croda Nera Hi GTX | Men's", C.FOOTWEAR),
        ("Transporter Duffel 65", C.BAGS),
        ("Norm Hat - TNF Black", C.ACCESSORIES),
        ("Baitjunkie 4.2 Inch Minnow Soft Bait", C.OTHER),
        ("", C.OTHER),
    ],
)
def test_title_decides_when_a_store_gives_no_product_type(title, expected):
    assert classify_product(None, title) is expected


def test_short_sleeves_are_never_shorts():
    assert classify_product("Short Sleeve Shirts", "Patagonia Men's Go To Shirt") is C.TOPS
    assert classify_product(None, "UA Tech Textured Men's Short Sleeve") is C.TOPS
    assert classify_product("SIS Gel", "SiS GO Isotonic Energy Gel - Short Dated") is C.OTHER


def test_title_refines_a_broad_tops_type_into_a_warm_layer_or_jacket():
    assert classify_product("Tops - L/S,L/S Pullovers", "Men's P-6 Logo Uprisal Hoody") is C.MIDLAYERS
    assert classify_product("Tops", "Firebird Track Top") is C.JACKETS
    # A title never moves a top into an unrelated category.
    assert classify_product("T-Shirts", "Shorts and Tee Set Kids") is C.TOPS
    assert classify_product("Clothing>Tops>Long Sleeved T-Shirts", "Echo Men's Sun Hoodie") is C.TOPS


def test_base_layers_follow_the_garment_they_name():
    assert classify_product("Baselayer", "LIFA MERINO MIDWEIGHT PANT, Black") is C.PANTS
    assert classify_product("Baselayer", "LIFA MERINO KNITEVO CREW, Black") is C.TOPS
    assert classify_product("Baselayer", "JR LIFA MERINO MIDWEIGHT SET, Navy") is C.TOPS
    assert classify_product("Clothing>Underwear>Upper Body", "Capilene Men's Midweight Crew") is C.TOPS
    assert classify_product("Clothing>Underwear>Lower Body", "Capilene Men's Midweight Bottoms") is C.PANTS


def test_equipment_is_other_even_when_its_title_names_a_garment():
    assert classify_product("Gear>Tents & Shelters>Accessories", "Lyra II Tent Footprint") is C.OTHER
    assert classify_product("Gear>Dog Gear", "Overcoat Jacket") is C.OTHER
    assert classify_product("Dog Coat", "Ruffwear Overcoat - Dog Jacket") is C.OTHER
    assert classify_product("Food & Hydration>Hydration>Hydration Accessories", "Hot Cap Accessory") is C.OTHER
    assert classify_product("Gear>Sleeping>Sleeping Bags", "Trek -1 Down Sleeping Bag") is C.OTHER
    assert classify_product("Snow Helmets", "Smith Survey Visor MIPS Snow Helmet") is C.OTHER
    assert classify_product("Non-Apparel,Wetsuits", "Men's R2 Yulex Front-Zip Full Suit") is C.OTHER
