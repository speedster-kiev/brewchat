import dataclasses

import pytest
from anthropic.lib.tools import ToolError

from brewchat.agent.models import OrderListItem
from brewchat.agent.session import Session
from brewchat.agent.tools import HANDOFF_NOTE, ToolContext, build_order_list_impl

PILS = "/shop/286-pilsner-malt/102-pilsner-malt-ebc-3-5-ebc-1-kg/"
US05 = "/shop/8-gaer/301-fermentis-safale-us-05-11-5-g-dry-yeast/"
CITRA = "/shop/51-amerikanske-humle-pellets/205-citra-hop-pellets-100-g-alpha-12-5/"
MOSAIC = "/shop/51-amerikanske-humle-pellets/207-mosaic-hop-pellets-100-g-alpha-12-0/"


def items() -> list[OrderListItem]:
    return [
        OrderListItem.model_validate({
            "ingredient": {"type": "fermentable", "name": "Pilsner malt", "amount": 4.5, "unit": "kg"},
            "product_handle": PILS, "quantity": 5, "source": "matched",
        }),
        OrderListItem.model_validate({
            "ingredient": {"type": "hop", "name": "Galaxy", "amount": 50, "unit": "g", "spec": "15% AA"},
            "product_handle": CITRA, "quantity": 1, "source": "substituted",
            "substitution": {"original_name": "Galaxy", "reason": "Galaxy is sold out; Citra is similar",
                             "confidence": "medium"},
        }),
        OrderListItem.model_validate({
            "ingredient": {"type": "hop", "name": "Amarillo", "amount": 30, "unit": "g"},
            "product_handle": MOSAIC, "quantity": 1, "source": "user_override",
            "substitution": {"original_name": "Amarillo", "reason": "user asked for Mosaic",
                             "confidence": "high"},
        }),
        OrderListItem.model_validate({
            "ingredient": {"type": "other", "name": "Whirlfloc", "amount": 1, "unit": "tablet"},
            "quantity": 1, "source": "unavailable",
        }),
        OrderListItem.model_validate({
            "ingredient": {"type": "yeast", "name": "US-05", "amount": 1, "unit": "pack"},
            "product_handle": US05, "quantity": 2, "source": "matched",
        }),
    ]


@pytest.fixture
def ctx(settings, catalog_cache) -> ToolContext:
    return ToolContext.from_settings(settings)


def test_links_are_base_url_plus_handle(ctx, settings):
    order = build_order_list_impl(ctx, Session(), items())
    base = settings.supplier.base_url.rstrip("/")
    by_name = {line.ingredient_name: line for line in order.items}
    assert by_name["Pilsner malt"].url == base + PILS
    assert by_name["Galaxy"].url == base + CITRA
    assert by_name["Amarillo"].url == base + MOSAIC


def test_total_with_vat(ctx):
    order = build_order_list_impl(ctx, Session(), items())
    # 5*20.00 + 65.00 + 62.00 + 2*39.75
    assert order.total == pytest.approx(306.5)
    assert order.currency == "DKK"
    assert order.vat_note == "Prices shown include VAT."
    pils = next(line for line in order.items if line.ingredient_name == "Pilsner malt")
    assert pils.unit_price == 20.0 and pils.line_total == 100.0


def test_total_without_vat(settings, catalog_cache):
    ex = dataclasses.replace(settings, supplier=dataclasses.replace(settings.supplier, prices_include_vat=False))
    order = build_order_list_impl(ToolContext.from_settings(ex), Session(), items())
    # 5*16.00 + 52.00 + 49.60 + 2*31.80
    assert order.total == pytest.approx(245.2)
    assert order.vat_note == "Prices shown exclude VAT."


def test_text_lines_show_label_price_link(ctx, settings):
    order = build_order_list_impl(ctx, Session(), items())
    text = order.text
    base = settings.supplier.base_url.rstrip("/")
    assert "[matched] 5 x Pilsner Malt, ebc 3 - 5 EBC, 1 kg" in text
    assert "20.00 DKK each = 100.00 DKK" in text
    assert "[substituted for Galaxy] 1 x Citra hop pellets" in text
    assert "65.00 DKK each" in text
    assert "[your choice (replaces Amarillo)] 1 x Mosaic hop pellets" in text
    assert "[matched] 2 x Fermentis - SafAle US-05" in text
    for handle in (PILS, CITRA, MOSAIC, US05):
        assert f"Link: {base}{handle}" in text
    assert "Total: 306.50 DKK (Prices shown include VAT.)" in text


def test_text_ends_with_handoff_and_has_cache_line(ctx):
    order = build_order_list_impl(ctx, Session(), items())
    assert order.text.rstrip().endswith(HANDOFF_NOTE)
    assert order.handoff_note == HANDOFF_NOTE
    assert "cart" in HANDOFF_NOTE and "by hand" in HANDOFF_NOTE
    assert "Stock and prices as of 2026-09-17 06:00 UTC; stock may have changed since." in order.text
    assert order.cache_timestamp.startswith("2026-09-17T06:00:00")


def test_unavailable_line_listed_without_link(ctx):
    order = build_order_list_impl(ctx, Session(), items())
    line = next(line for line in order.items if line.source == "unavailable")
    assert line.url is None and line.product_handle is None and line.line_total is None
    text_lines = order.text.splitlines()
    idx = next(i for i, t in enumerate(text_lines) if "[unavailable] Whirlfloc" in t)
    assert "Link: none" in text_lines[idx + 1]
    assert "http" not in text_lines[idx] + text_lines[idx + 1]


def test_order_stored_on_session_and_session_log_bumped(ctx):
    session = Session()
    order = build_order_list_impl(ctx, session, items())
    assert session.order_list is order
    assert ctx.session_log.get(session.session_id)["lists_built"] == 1


def test_unknown_handle_is_tool_error(ctx):
    bad = items()
    bad[0] = bad[0].model_copy(update={"product_handle": "/shop/does-not-exist/"})
    session = Session()
    with pytest.raises(ToolError, match="search_catalog"):
        build_order_list_impl(ctx, session, bad)
    assert session.order_list is None


def test_out_of_stock_product_is_flagged_in_text(ctx):
    galaxy = OrderListItem.model_validate({
        "ingredient": {"type": "hop", "name": "Galaxy", "amount": 50, "unit": "g"},
        "product_handle": "/shop/54-australsk-humle/206-galaxy-hop-pellets-100-g-alpha-15-0/",
        "quantity": 1, "source": "matched",
    })
    order = build_order_list_impl(ctx, Session(), [galaxy])
    assert order.items[0].in_stock is False
    assert "out of stock" in order.text


# -- pack sizes (issue #2) ------------------------------------------------------

MO_100G = "/shop/6-malt/121-maris-otter-malt-northfield-maltings-ebc-5-7-pr-100-g/"
MO_25KG = "/shop/263-hele-25-kg-maltsaekke/122-maris-otter-malt-northfield-maltings-ebc-5-7-pr-25-kg/"
CITRA_300G = "/shop/51-amerikanske-humle-pellets/221-citra-hop-pellets-us-alpha-12-5-300-g/"


def _item(name, amount, unit, handle, quantity, type_="fermentable") -> OrderListItem:
    return OrderListItem.model_validate({
        "ingredient": {"type": type_, "name": name, "amount": amount, "unit": unit},
        "product_handle": handle, "quantity": quantity, "source": "matched",
    })


def test_priced_per_100_g_quantity_is_recalculated(ctx):
    # The bug from issue #2: the agent treats "pr. 100 g." as a bag and orders 1.
    order = build_order_list_impl(ctx, Session(), [_item("Maris Otter", 5, "kg", MO_100G, 1)])
    line = order.items[0]
    assert line.quantity == 50
    assert line.pack_size == "100 g"
    assert line.quantity_requested == 1
    assert line.line_total == pytest.approx(125.0)  # 50 * 2.50
    assert order.total == pytest.approx(125.0)
    assert "[matched] 50 x Maris Otter Malt" in order.text
    assert "For: Maris Otter (5 kg); buying 50 x 100 g = 5 kg" in order.text


def test_25_kg_sack_is_not_multiplied_by_kilos(ctx):
    # The mirror bug: quantity given in kilos for a per-sack price.
    order = build_order_list_impl(ctx, Session(), [_item("Maris Otter", 20, "kg", MO_25KG, 20)])
    line = order.items[0]
    assert line.quantity == 1 and line.quantity_requested == 20
    assert line.line_total == pytest.approx(425.0)
    assert "buying 1 x 25 kg = 25 kg" in order.text


@pytest.mark.parametrize(("amount", "handle", "expected"), [
    (40, CITRA, 1), (250, CITRA, 3), (250, CITRA_300G, 1),
])
def test_hop_packs_rounded_up(ctx, amount, handle, expected):
    order = build_order_list_impl(ctx, Session(), [_item("Citra", amount, "g", handle, 1, "hop")])
    assert order.items[0].quantity == expected


def test_correct_quantity_is_not_flagged(ctx):
    order = build_order_list_impl(ctx, Session(), [_item("Maris Otter", 5, "kg", MO_100G, 50)])
    assert order.items[0].quantity == 50 and order.items[0].quantity_requested is None


def test_per_pack_items_keep_the_agents_quantity(ctx):
    # "1 pack" of an 11,5 g sachet can't be compared, so the agent's 2 stands.
    order = build_order_list_impl(ctx, Session(), [_item("US-05", 1, "pack", US05, 2, "yeast")])
    line = order.items[0]
    assert line.quantity == 2 and line.quantity_requested is None
    assert line.line_total == pytest.approx(79.5)
