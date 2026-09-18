import csv
import json

import pytest

from brewchat.agent.models import OrderListItem
from brewchat.agent.session import Session
from brewchat.agent.tools import ToolContext, build_order_list_impl
from brewchat.logs.export import export_csv
from brewchat.logs.substitution_log import FIELDS

PILS = "/shop/286-pilsner-malt/102-pilsner-malt-ebc-3-5-ebc-1-kg/"
CITRA = "/shop/51-amerikanske-humle-pellets/205-citra-hop-pellets-100-g-alpha-12-5/"
MOSAIC = "/shop/51-amerikanske-humle-pellets/207-mosaic-hop-pellets-100-g-alpha-12-0/"
NELSON = "/shop/55-new-zealand/212-nelson-sauvin-hop-pellets-100-g-alpha-12-0/"
CENTENNIAL = "/shop/51-amerikanske-humle-pellets/201-centennial-hop-pellets-100-g-alpha-9-5/"


def matched_pils() -> OrderListItem:
    return OrderListItem.model_validate({
        "ingredient": {"type": "fermentable", "name": "Pilsner malt", "amount": 4, "unit": "kg"},
        "product_handle": PILS, "quantity": 4, "source": "matched",
    })


def hop(name: str, source: str, handle: str | None = None, spec: str | None = None) -> OrderListItem:
    data = {
        "ingredient": {"type": "hop", "name": name, "amount": 50, "unit": "g", "spec": spec},
        "product_handle": handle, "quantity": 1, "source": source,
    }
    if source in ("substituted", "user_override"):
        data["substitution"] = {"original_name": name, "reason": f"{name} is sold out", "confidence": "medium"}
    return OrderListItem.model_validate(data)


@pytest.fixture
def ctx(settings, catalog_cache) -> ToolContext:
    return ToolContext.from_settings(settings)


def records(ctx) -> list[dict]:
    return ctx.substitution_log.read_all()


def test_one_record_per_substitution(ctx):
    session = Session()
    build_order_list_impl(ctx, session, [
        matched_pils(),
        hop("Galaxy", "substituted", CITRA, spec="15% AA"),
        hop("Amarillo", "substituted", CENTENNIAL),
    ])
    recs = records(ctx)
    assert len(recs) == 2
    assert all(set(r) == set(FIELDS) for r in recs)
    galaxy = next(r for r in recs if r["original_name"] == "Galaxy")
    assert galaxy["session_id"] == session.session_id
    assert galaxy["original_spec"] == "15% AA"
    assert galaxy["proposed_handle"] == CITRA
    assert galaxy["proposed_title"].startswith("Citra hop pellets")
    assert galaxy["decision"] == "accepted"
    assert galaxy["final_handle"] == CITRA
    assert galaxy["confidence"] == "medium"
    assert galaxy["cache_timestamp"].startswith("2026-09-17T06:00:00")
    assert session.proposals["Galaxy"]["handle"] == CITRA


def test_rebuild_with_unavailable_records_rejected(ctx):
    session = Session()
    build_order_list_impl(ctx, session, [matched_pils(), hop("Galaxy", "substituted", CITRA)])
    build_order_list_impl(ctx, session, [matched_pils(), hop("Galaxy", "unavailable")])
    recs = [r for r in records(ctx) if r["original_name"] == "Galaxy"]
    assert [r["decision"] for r in recs] == ["accepted", "rejected"]
    assert recs[-1]["proposed_handle"] == CITRA
    assert recs[-1]["final_handle"] is None
    latest = ctx.substitution_log.read_latest()
    assert len(latest) == 1 and latest[0]["decision"] == "rejected"


def test_unavailable_without_prior_proposal_writes_nothing(ctx):
    build_order_list_impl(ctx, Session(), [matched_pils(), hop("Galaxy", "unavailable")])
    assert records(ctx) == []


def test_replaced_by_user(ctx):
    session = Session()
    build_order_list_impl(ctx, session, [hop("Galaxy", "substituted", CITRA)])
    build_order_list_impl(ctx, session, [hop("Galaxy", "user_override", NELSON)])
    last = records(ctx)[-1]
    assert last["decision"] == "replaced_by_user"
    assert last["proposed_handle"] == CITRA  # what the agent proposed
    assert last["final_handle"] == NELSON  # what the user chose


def test_log_never_contains_base_url(ctx, settings):
    session = Session()
    build_order_list_impl(ctx, session, [matched_pils(), hop("Galaxy", "substituted", CITRA)])
    build_order_list_impl(ctx, session, [matched_pils(), hop("Galaxy", "user_override", MOSAIC)])
    build_order_list_impl(ctx, session, [matched_pils(), hop("Galaxy", "unavailable")])
    raw = ctx.substitution_log.path.read_text(encoding="utf-8")
    assert raw.strip()
    assert settings.supplier.base_url not in raw
    assert "hopcellar.example" not in raw
    assert "http" not in raw
    for line in raw.splitlines():
        json.loads(line)


def test_export_csv_one_row_per_session_and_original(ctx, tmp_path):
    s1, s2 = Session(), Session()
    build_order_list_impl(ctx, s1, [hop("Galaxy", "substituted", CITRA), hop("Amarillo", "substituted", CENTENNIAL)])
    build_order_list_impl(ctx, s1, [hop("Galaxy", "unavailable"), hop("Amarillo", "substituted", CENTENNIAL)])
    build_order_list_impl(ctx, s2, [hop("Galaxy", "substituted", CITRA)])
    build_order_list_impl(ctx, s2, [hop("Galaxy", "user_override", NELSON)])
    assert len(records(ctx)) == 6

    out = tmp_path / "export" / "subs.csv"
    n = export_csv(ctx.substitution_log.path, out)
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert n == len(rows) == 3
    final = {(r["session_id"], r["original_name"]): r for r in rows}
    assert final[(s1.session_id, "Galaxy")]["decision"] == "rejected"
    assert final[(s1.session_id, "Galaxy")]["final_handle"] == ""
    assert final[(s1.session_id, "Amarillo")]["decision"] == "accepted"
    assert final[(s2.session_id, "Galaxy")]["decision"] == "replaced_by_user"
    assert final[(s2.session_id, "Galaxy")]["final_handle"] == NELSON
    assert list(rows[0]) == list(FIELDS)
