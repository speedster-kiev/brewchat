import pytest

from brewchat.catalog.pack import parse_pack_size, units_needed

# Synthetic titles in the shapes the shop uses (issue #2); never real titles.
TITLES = [
    ("Pilsner malt - Northfield Maltings, ebc 3 - 5, pr. 100 g.", "100 g", 100.0),
    ("Melanoidin malt - Northfield Maltings, ebc 60 - 80 pr. 100 g.", "100 g", 100.0),
    ("Pilsner malt - Northfield Maltings, ebc 3 - 5, pr. 25 kg.", "25 kg", 25000.0),
    ("Bohemian Pilsner malt - Northfield Maltings, ebc 3 - 5 EBC, pr. 25 kg sæk", "25 kg", 25000.0),
    ("Organic Pilsner malt - Farm Maltings, ebc 3 - 4, pr. 25 kg", "25 kg", 25000.0),
    ("Crushing service - max 25 kg. (order 1 pr. started 25 kg)", "25 kg", 25000.0),
    ("Saaz, 2025 pellets CZ, alpha 4,1% 300 g.", "300 g", 300.0),
    ("Citra, 2024 pellets US, alpha 12,9%, 100 g.", "100 g", 100.0),
    ("Nelson Sauvin, 2024 pellets NZ, alpha 9,6%, 100 g", "100 g", 100.0),
    ("Candi sugar dark 1000 g.", "1000 g", 1000.0),
    ("Muscovado sugar 1 kg", "1 kg", 1000.0),
    ("Fermentis - SafAle US-05, 11,5 g. dry yeast", "11.5 g", 11.5),
    ("Lactic acid 80%, 100 ml", "100 ml", 100.0),
    ("Raspberry puree 1 liter", "1 l", 1000.0),
    ("Glucose syrup 2 Ltr", "2 l", 2000.0),
    ("Crystal Malt 150 EBC (approx. 60L), 1 kg", "1 kg", 1000.0),
]


@pytest.mark.parametrize(("title", "label", "base"), TITLES)
def test_parse_pack_size(title, label, base):
    pack = parse_pack_size(title)
    assert pack is not None
    assert pack.label == label
    assert pack.base_amount == pytest.approx(base)


@pytest.mark.parametrize("title", [
    "White Labs WLP001 California Ale, liquid yeast",
    "Whirlfloc tablets 20 stk.",  # count packs are left to the agent
    "Caramel Malt 60L",  # Lovibond, not litres
    "Hop sock, stainless steel",
    "",
])
def test_no_pack_size(title):
    assert parse_pack_size(title) is None


@pytest.mark.parametrize(("amount", "unit", "title", "expected"), [
    (5, "kg", "Pilsner malt, pr. 100 g.", 50),
    (4.5, "kg", "Pilsner malt, pr. 100 g.", 45),
    (0.3, "kg", "Pilsner malt, pr. 100 g.", 3),  # float noise must not add a unit
    (20, "kg", "Pilsner malt, pr. 25 kg.", 1),
    (26, "kg", "Pilsner malt, pr. 25 kg.", 2),
    (40, "g", "Citra, alpha 12,9%, 100 g.", 1),
    (250, "g", "Citra, alpha 12,9%, 100 g.", 3),
    (250, "g", "Citra, alpha 12,9% 300 g.", 1),
    (8, "oz", "Citra, alpha 12,9%, 100 g.", 3),  # 226.8 g
    (11, "lb", "Pale malt, pr. 100 g.", 50),  # 4989.5 g
    (500, "ml", "Raspberry puree 1 liter", 1),
])
def test_units_needed(amount, unit, title, expected):
    assert units_needed(amount, unit, parse_pack_size(title)) == expected


@pytest.mark.parametrize(("amount", "unit", "title"), [
    (1, "pack", "Fermentis - SafAle US-05, 11,5 g. dry yeast"),  # count vs mass
    (1, "tsp", "Gypsum, 100 g"),  # volume vs mass
    (2, "pack", "White Labs WLP001 California Ale, liquid yeast"),  # no pack size
    (1, "whatever", "Pilsner malt, pr. 100 g."),
])
def test_units_needed_incomparable_is_none(amount, unit, title):
    assert units_needed(amount, unit, parse_pack_size(title)) is None
