import pytest

from brewchat.catalog.origin import (
    normalize_code,
    producer_candidate,
    product_origin,
    split_query_origins,
    style_origins,
)

PRODUCERS = {"Castle Malting": "BE", "Weyermann": "DE", "Crisp Malting": "GB"}


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Pilsner malt - Castle Malting, ebc 3 - 5, pr. 100 g.", "BE"),
        ("pilsner malt - WEYERMANN, ebc 2 - 4", "DE"),
        ("Maris Otter - Crisp Malting, ebc 5", "GB"),
        # Hops carry a bare country token.
        ("Fuggles UK, 2025 pellets, alpha 3,9%, 100 g.", "GB"),
        ("Saaz, 2025 pellets CZ, alpha 4,1% 100 g.", "CZ"),
        ("Galaxy, 2025 pellets AUS, alpha 16,1%, 100 g.", "AU"),
        ("Bobek Styrian Goldings SL, 2025 pellets, alpha 3%, 100 g.", "SI"),
        # "ES Økologisk" is not known to be a country, so it is not read as one.
        ("Cascade ES Økologisk, 2025 pellets, alpha 7%, 100 g.", None),
        # The producer wins over a token.
        ("Pilsner malt - Weyermann, ebc 2 - 4, pellets UK", "DE"),
        # No signal, no origin.
        ("Irish Moss 100 g.", None),
        ("", None),
    ],
)
def test_product_origin(title, expected):
    assert product_origin(title, PRODUCERS) == expected


def test_title_token_ignores_product_codes_and_hyphenated_words():
    # "US-05" is a yeast strain, "Columbus" merely contains "us".
    assert product_origin("Fermentis - SafAle US-05, 11,5 g.", PRODUCERS) is None
    assert product_origin("Columbus (CTZ), pellets, 100 g.", PRODUCERS) is None
    assert product_origin("Fermentis - SafLager W-34/70, 11,5 g.", PRODUCERS) is None


def test_producer_map_is_optional():
    assert product_origin("Pilsner malt - Castle Malting") is None
    assert product_origin("Mosaic US, 2024 pellets") == "US"


@pytest.mark.parametrize(
    ("query", "text", "origins"),
    [
        ("Belgian Pilsner malt", "Pilsner malt", {"BE"}),
        ("german pilsner", "pilsner", {"DE"}),
        ("UK Fuggles", "Fuggles", {"GB"}),
        ("Citra, American", "Citra", {"US"}),
        ("New Zealand Nelson Sauvin", "Nelson Sauvin", {"NZ"}),
        ("Pilsner malt", "Pilsner malt", set()),
        # Words that are not origins here.
        ("US-05", "US-05", set()),
        ("Irish moss", "Irish moss", set()),
        ("Mangrove Jack's M44 US West Coast", "Mangrove Jack's M44 US West Coast", set()),
        ("Danish-style lager yeast", "Danish-style lager yeast", set()),
    ],
)
def test_split_query_origins(query, text, origins):
    assert split_query_origins(query) == (text, frozenset(origins))


def test_query_of_only_an_origin_word_keeps_its_text():
    assert split_query_origins("Belgian") == ("Belgian", frozenset({"BE"}))


@pytest.mark.parametrize(("value", "code"), [("be", "BE"), ("UK", "GB"), (" dk ", "DK"), ("Denmark", None), ("", None)])
def test_normalize_code(value, code):
    assert normalize_code(value) == code


@pytest.mark.parametrize(
    ("title", "producer"),
    [
        ("Økologisk Pilsner malt - Gyrup Gårdmalt, ebc 3 - 4, pr. 25 kg", "Gyrup Gårdmalt"),
        ("Maris Otter - Crisp, ebc 5 - 7, pr. 100 g.", "Crisp"),
        ("Pilsner malt, ebc 3 - 5 - 6, 1 kg", None),  # a spec range, not a producer
        ("Pilsner malt - ebc 3 - 5", None),
        ("Irish Moss 100 g.", None),
    ],
)
def test_producer_candidate(title, producer):
    assert producer_candidate(title) == producer


STYLES = {"helles": ("DE",), "pilsner": ("DE", "CZ"), "esb": ("GB",), "kölsch": ("DE",)}


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ("Munich Helles", {"DE"}),
        ("helles", {"DE"}),
        ("Kölsch", {"DE"}),
        ("Pilsner", {"DE", "CZ"}),
        ("American Pale Ale", {"US"}),  # origin word in the style, no table entry needed
        ("Czech Pilsner", {"CZ"}),  # the word beats the table's DE + CZ
        ("Belgian Blond Ale", {"BE"}),
        ("Cornelius Ale", set()),
        ("Shellhelles", set()),  # whole words only
        ("", set()),
        (None, set()),
    ],
)
def test_style_origins(style, expected):
    assert style_origins(style, STYLES) == frozenset(expected)


def test_style_origins_without_a_table_only_reads_origin_words():
    assert style_origins("Munich Helles", None) == frozenset()
    assert style_origins("English Bitter", {}) == frozenset({"GB"})


def test_longest_style_key_wins():
    table = {"pilsner": ("DE",), "imperial pilsner": ("US",)}
    assert style_origins("Imperial Pilsner", table) == frozenset({"US"})
