"""The agent's three tools and the order-list logic behind them (plan.md, step 4).

Tools reach per-session state through ``brewchat.agent.session.current_session``
(set by the runner), never through a model-supplied argument. Invalid tool
input and recoverable problems (unknown handle, no session) are raised as
``ToolError`` so the tool runner hands them back to the model as an
``is_error`` tool result instead of breaking the loop.
"""

import functools
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pydantic
from anthropic import beta_tool
from anthropic.lib.tools import ToolError

from brewchat.agent.models import (
    Ingredient,
    IngredientType,
    OrderLine,
    OrderList,
    OrderListItem,
)
from brewchat.agent.session import Session, current_session
from brewchat.catalog.pack import units_needed
from brewchat.catalog.search import CatalogIndex
from brewchat.config import Settings, staple_key
from brewchat.telemetry.session_log import SessionLog
from brewchat.telemetry.substitution_log import SubstitutionLog
from brewchat.units import normalize_unit

SUGAR_TYPES = ("fermentable", "other")

HANDOFF_NOTE = (
    "BrewChat cannot add anything to your cart: the shop has no cart link or API. "
    "Open each link above and add the items to the shop's cart by hand, then check out on the shop's site."
)


@dataclass
class ToolContext:
    settings: Settings
    index: CatalogIndex
    substitution_log: SubstitutionLog
    session_log: SessionLog

    @classmethod
    def from_settings(cls, settings: Settings) -> "ToolContext":
        return cls(
            settings=settings,
            index=CatalogIndex(
                settings.cache_path,
                settings.supplier.prices_include_vat,
                settings.origins,
                settings.style_origins,
            ),
            substitution_log=SubstitutionLog(settings.logs_dir / "substitutions.jsonl"),
            session_log=SessionLog(settings.logs_dir / "sessions.sqlite"),
        )


# -- helpers ----------------------------------------------------------------


def _fmt_qty(q: float) -> str:
    return str(int(q)) if float(q).is_integer() else f"{q:g}"


def _fmt_money(v: float, currency: str) -> str:
    return f"{v:.2f} {currency}"


def format_cache_timestamp(ts: datetime) -> str:
    utc = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
    return f"Stock and prices as of {utc:%Y-%m-%d %H:%M} UTC; stock may have changed since."


def _fmt_amount(amount: float, unit: str) -> str:
    return f"{_fmt_qty(amount)} {unit}".strip()


def _bought(line: OrderLine) -> str:
    """ "; buying 50 x 100 g = 5 kg" for lines sold by weight or volume, else ""."""
    if not line.pack_size:
        return ""
    amount_str, unit = line.pack_size.split(" ", 1)
    total = float(amount_str) * line.quantity
    if unit in ("g", "ml") and total >= 1000:
        total, unit = total / 1000, {"g": "kg", "ml": "l"}[unit]
    return f"; buying {_fmt_qty(line.quantity)} x {line.pack_size} = {_fmt_amount(round(total, 3), unit)}"


def quantity_notes(order: OrderList) -> list[str]:
    """One line per quantity the code recalculated from the pack size."""
    return [
        f"{line.ingredient_name}: quantity {_fmt_qty(line.quantity_requested)} -> {_fmt_qty(line.quantity)}"
        f" ({_fmt_amount(line.ingredient_amount, line.ingredient_unit)} needed, sold per {line.pack_size})"
        for line in order.items
        if line.quantity_requested is not None
    ]


def record_alternatives(session: Session, candidates: list[dict[str, Any]]) -> None:
    """Remember, per handle, the other in-stock candidates that tie with it at the top score.

    Only a search whose suggestion was one of several equally good matches counts:
    that is a choice the user should see, whichever of them ends up on the list.
    """
    if not candidates:
        return
    top = [c for c in candidates if c["score"] == candidates[0]["score"] and c["in_stock"]]
    if len(top) < 2 or not any(c["suggested"] for c in top):
        return
    for c in top:
        session.alternatives[c["handle"]] = [o for o in top if o is not c]


def _fmt_candidate(c: dict[str, Any], currency: str) -> str:
    origin = c["origin"] or "origin unstated"
    per = f" per {c['pack_size']}" if c["pack_size"] else ""
    return f"{c['title']} ({origin}, {_fmt_money(c['price'], currency)}{per})"


def alternative_notes(order: OrderList, session: Session) -> list[str]:
    """One line per matched line that had equally good in-stock alternatives in its search."""
    notes = []
    for line in order.items:
        others = session.alternatives.get(line.product_handle or "") if line.source == "matched" else None
        if others:
            alts = "; ".join(_fmt_candidate(c, order.currency) for c in others)
            notes.append(f"{line.ingredient_name} (listed: {line.product_title}): {alts}")
    return notes


def _source_label(line: OrderLine) -> str:
    original = line.substitution.original_name if line.substitution else line.ingredient_name
    return {
        "matched": "matched",
        "substituted": f"substituted for {original}",
        "user_override": f"your choice (replaces {original})",
        "unavailable": "unavailable",
    }[line.source]


def _validation_message(tool_name: str, exc: pydantic.ValidationError) -> str:
    parts = []
    for err in exc.errors(include_url=False, include_input=False):
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"- {loc or '(input)'}: {err.get('msg')}")
    return f"Invalid input for {tool_name}; nothing was saved. Fix these and call it again:\n" + "\n".join(parts)


def _report_validation_errors(tool: Any) -> Any:
    """Make pydantic validation failures come back to the model with details.

    ``BetaFunctionTool.call`` turns a ``ValidationError`` into a bare
    ``ValueError("Invalid arguments ...")``; wrapping the validated function
    re-raises it as a ``ToolError`` listing each bad field instead.
    """
    validated = tool._func_with_validate

    @functools.wraps(validated)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return validated(*args, **kwargs)
        except pydantic.ValidationError as exc:
            raise ToolError(_validation_message(tool.name, exc)) from exc

    tool._func_with_validate = wrapper
    return tool


def _session() -> Session:
    try:
        return current_session.get()
    except LookupError:
        raise ToolError("No active session; this tool can only run inside a chat turn.") from None


# -- pure logic -------------------------------------------------------------

_CANONICAL_UNIT = {"mass": "g", "volume": "ml"}


def _same_ingredient(a: OrderListItem, b: OrderListItem) -> bool:
    def key(it: OrderListItem) -> tuple[str, str]:
        return it.ingredient.type, " ".join(it.ingredient.name.lower().split())

    return key(a) == key(b) or (a.product_handle is not None and a.product_handle == b.product_handle)


def _combine(a: OrderListItem, b: OrderListItem) -> OrderListItem | None:
    """``a`` with ``b``'s amount added, or ``None`` if their units can't be added."""
    fam_a, base_a = normalize_unit(a.ingredient.amount, a.ingredient.unit)
    fam_b, base_b = normalize_unit(b.ingredient.amount, b.ingredient.unit)
    if fam_a != fam_b:
        return None
    if a.ingredient.unit.strip().lower() == b.ingredient.unit.strip().lower() or fam_a not in _CANONICAL_UNIT:
        amount, unit = a.ingredient.amount + b.ingredient.amount, a.ingredient.unit
    else:
        amount, unit = round(base_a + base_b, 6), _CANONICAL_UNIT[fam_a]
    parts = [p for t in (a.ingredient.timing, b.ingredient.timing) if t for p in t.split(", ")]
    timing = ", ".join(dict.fromkeys(parts)) or None
    ingredient = a.ingredient.model_copy(update={"amount": amount, "unit": unit, "timing": timing})
    return a.model_copy(update={"ingredient": ingredient, "quantity": a.quantity + b.quantity})


def merge_duplicate_items(items: list[OrderListItem]) -> list[OrderListItem]:
    """Merge repeated additions of one ingredient into a single item (issue #10).

    Items match on type + name (case/whitespace-insensitive) or on product handle.
    The first item's product, source and substitution win; amounts are summed
    across compatible units so the pack quantity is computed once from the total,
    and quantities are summed for per-pack items. Items whose units can't be
    added (e.g. "1 pack" and "11 g") stay separate.
    """
    merged: list[OrderListItem] = []
    for it in items:
        for i, prev in enumerate(merged):
            if _same_ingredient(prev, it) and (combined := _combine(prev, it)) is not None:
                merged[i] = combined
                break
        else:
            merged.append(it)
    return merged


def left_out_staples(settings: Settings, session: Session, items: list[OrderListItem]) -> list[str]:
    """Staple names from the recorded parse plus any staple line the model sent, once each, in recipe order."""
    names = [ing.name for ing in session.parsed_recipe or []] + [it.ingredient.name for it in items]
    left_out: list[str] = []
    for name in names:
        if settings.is_staple(name) and staple_key(name) not in left_out:
            left_out.append(staple_key(name))
    return left_out


def render_text(order: OrderList, supplier_name: str, cache_line: str) -> str:
    out: list[str] = [f"Order list for {supplier_name}", ""]
    for n, line in enumerate(order.items, 1):
        need = f"{_fmt_qty(line.ingredient_amount)} {line.ingredient_unit}".strip()
        label = _source_label(line)
        if line.source == "unavailable":
            out.append(f"{n}. [{label}] {line.ingredient_name} ({need}): not available here, source it elsewhere.")
            out.append("   Link: none")
            continue
        price = (
            f"{_fmt_money(line.unit_price, order.currency)} each = {_fmt_money(line.line_total, order.currency)}"
            if line.unit_price is not None and line.line_total is not None
            else "price unknown"
        )
        stock = "" if line.in_stock else " (out of stock at last sync)"
        out.append(f"{n}. [{label}] {_fmt_qty(line.quantity)} x {line.product_title}{stock}, {price}")
        out.append(f"   For: {line.ingredient_name} ({need}){_bought(line)}")
        if line.substitution and line.source == "substituted":
            out.append(f"   Why: {line.substitution.reason} (confidence: {line.substitution.confidence})")
        out.append(f"   Link: {line.url}")
    if order.left_out:
        out += ["", f"Left out, assumed on hand: {', '.join(order.left_out)}"]
    out += [
        "",
        f"Total: {_fmt_money(order.total, order.currency)} ({order.vat_note})",
        cache_line,
        "",
        order.handoff_note,
    ]
    return "\n".join(out)


def build_order_list_impl(ctx: ToolContext, session: Session, items: list[OrderListItem]) -> OrderList:
    """Resolve handles, merge repeated ingredients, price and render the list, store it, write the logs."""
    if not items:
        raise ToolError("build_order_list needs at least one item (one per recipe ingredient).")
    settings = ctx.settings
    # Pantry staples are assumed on hand: no line, no substitution record (issue #4), but the
    # list says which were left out, from the parse even when the model omitted them (issue #14).
    left_out = left_out_staples(settings, session, items)
    items = [it for it in items if not settings.is_staple(it.ingredient.name)]
    include_vat = settings.supplier.prices_include_vat
    base_url = settings.supplier.base_url.rstrip("/")

    unknown = [
        f"{it.ingredient.name}: {it.product_handle!r}"
        for it in items
        if it.source != "unavailable" and ctx.index.get(it.product_handle or "") is None
    ]
    if unknown:
        raise ToolError(
            "Unknown product handle(s), nothing was built: "
            + "; ".join(unknown)
            + ". Call search_catalog again and use a handle exactly as it returns it."
        )

    items = merge_duplicate_items(items)
    lines: list[OrderLine] = []
    for it in items:
        product = None if it.source == "unavailable" else ctx.index.get(it.product_handle or "")
        if product is None:
            lines.append(OrderLine(
                ingredient_name=it.ingredient.name,
                ingredient_amount=it.ingredient.amount,
                ingredient_unit=it.ingredient.unit,
                source=it.source,
                product_title=None,
                product_handle=None,
                url=None,
                quantity=it.quantity,
                unit_price=None,
                line_total=None,
                in_stock=None,
                substitution=it.substitution,
            ))
            continue
        unit_price = product.price(include_vat)
        pack = product.pack_size()
        quantity = units_needed(it.ingredient.amount, it.ingredient.unit, pack) or it.quantity
        lines.append(OrderLine(
            ingredient_name=it.ingredient.name,
            ingredient_amount=it.ingredient.amount,
            ingredient_unit=it.ingredient.unit,
            source=it.source,
            product_title=product.title,
            product_handle=product.handle,
            url=base_url + product.handle,
            quantity=quantity,
            pack_size=pack.label if pack else None,
            quantity_requested=None if quantity == it.quantity else it.quantity,
            unit_price=unit_price,
            line_total=round(unit_price * quantity, 2),
            in_stock=product.in_stock(),
            substitution=it.substitution,
        ))

    cache_ts = ctx.index.cache_timestamp
    order = OrderList(
        items=lines,
        total=round(sum(line.line_total or 0.0 for line in lines), 2),
        currency=settings.supplier.currency,
        vat_note=settings.supplier.vat_note,
        cache_timestamp=cache_ts.isoformat(),
        handoff_note=HANDOFF_NOTE,
        left_out=left_out,
        text="",
    )
    order.text = render_text(order, settings.supplier.name, format_cache_timestamp(cache_ts))

    session.order_list = order
    ctx.substitution_log.record_build(session, items, lines, order.cache_timestamp)
    ctx.session_log.record_list_built(session.session_id)
    return order


# -- tools ------------------------------------------------------------------


def make_tools(ctx: ToolContext) -> list:
    """The three agent tools, bound to ``ctx``; the session comes from ``current_session``."""

    @beta_tool
    def submit_parsed_recipe(ingredients: list[Ingredient], style: str | None = None) -> str:
        """Record the recipe, parsed into standardized ingredients, before any catalog lookup.

        Call this once per recipe (again if the user corrects the recipe), with one entry per
        ingredient line exactly as the recipe states it: type (fermentable, hop, yeast or other),
        name, amount and unit, timing if given (e.g. "60 min", "dry hop day 3"), and spec in the
        recipe's own words (e.g. "8% AA", "120 EBC", "75% attenuation"). Water is not an ingredient
        to buy; leave it out. If the same hop appears at several timings, list each addition.
        This is a checkpoint: search the catalog only after it succeeds.

        Args:
            ingredients: Every purchasable ingredient in the recipe, in recipe order.
            style: The beer style if the recipe names or clearly implies one (e.g. "Munich Helles",
                "American Pale Ale"), else omit. Later searches use it to put ingredients from the
                style's usual country first; it never hides a candidate.
        """
        if not ingredients:
            raise ToolError("ingredients is empty: include every purchasable ingredient from the recipe.")
        session = _session()
        session.parsed_recipe = list(ingredients)
        session.style = (style or "").strip() or None
        session.alternatives = {}
        counts: dict[str, int] = {}
        for ing in ingredients:
            counts[ing.type] = counts.get(ing.type, 0) + 1
        breakdown = ", ".join(f"{n} {t}" for t, n in counts.items())
        # Pantry staples stay in the recorded parse (the parse eval scores it) but are not named
        # to the model: it is asked to source only the rest, and build_order_list notes them (issue #14).
        to_source = list(dict.fromkeys(i.name for i in ingredients if not ctx.settings.is_staple(i.name)))
        reply = f"Recorded {len(ingredients)} ingredients ({breakdown})."
        if not to_source:
            return reply + " Nothing to search for or list: everything in it is assumed on hand."
        return reply + f" Now search the catalog for each of these and list them: {'; '.join(to_source)}."

    @beta_tool
    def search_catalog(
        query: str, ingredient_type: IngredientType | None = None, limit: int = 5, style: str | None = None
    ) -> str:
        """Fuzzy-search the shop's ingredient catalog by product title; returns candidates, you pick.

        Titles embed the spec text (EBC colour, alpha acid %, yeast strain), so read them the way a
        brewer would. Each candidate has handle, title, score (0-100, higher is closer), in_stock,
        price, pack_size, ingredient_type, origin, suggested and why. price is for one unit of pack_size (e.g. "100 g"
        or "25 kg"); pack_size null means the price is per pack. origin is the ISO country code of the
        maltster or hop ("BE", "DE", "DK", "GB", "US", ...), or null when the shop's title doesn't say: null
        means unknown, not "none". Put a country in the query when the recipe asks for one ("Belgian Pilsner
        malt", "UK Fuggles"): it is taken out of the text match and used only to put candidates from that
        country first among equally good ones, so still check the origin field. The recipe's style (from
        submit_parsed_recipe) does the same for that style's usual country, e.g. German malt for a helles.
        suggested=true marks the code's pick among the best matches (in stock, and from the asked-for or
        style's origin where that decides); why says what put it first, and is null when there was no
        tie to break. The other equally good candidates are real alternatives: nothing is filtered
        out, so the user can choose one. Out-of-stock products ARE returned, flagged
        in_stock=false, so you can see the right product exists but cannot be ordered: never put an
        out-of-stock product on the list without saying so. To substitute, search again for the
        substitute (by name, style or spec) and use only a handle this tool returned. An explicit
        "no match" message means nothing scored high enough: try another spelling or a broader
        query before giving up.

        Args:
            query: Ingredient name, optionally with spec words, e.g. "US-05", "Pilsner malt", "Citra".
            ingredient_type: Restrict to fermentable, hop, yeast or other. Use the recipe ingredient's type.
                fermentable and other both search fermentable and other products (sugars are filed
                under either).
            limit: Maximum candidates to return (1-20).
            style: Override the recipe's style for this search; normally omit it.
        """
        if ctx.settings.is_staple(query):
            # Belt-and-braces for issue #14: same exact-name rule as the parse, so "candi sugar" still searches.
            return (
                f'"{query}" is a pantry staple the brewer has on hand: build_order_list leaves it out and'
                " tells the user. Do not search for, list or substitute it. For a specific brewing sugar,"
                " search its own name."
            )
        session = current_session.get(None)
        if style is None:
            style = getattr(session, "style", None)
        if ingredient_type in SUGAR_TYPES:
            # Recipes call sugars fermentable; the shop files them under "other". Search both.
            hits = ctx.index.search(query, limit=20, style=style)
            results = [c for c in hits if c["ingredient_type"] in SUGAR_TYPES][: max(1, min(limit, 20))]
        else:
            results = ctx.index.search(query, ingredient_type=ingredient_type, limit=limit, style=style)
        if not results:
            scope = f" among {ingredient_type} products" if ingredient_type else ""
            return (
                f'No match for "{query}"{scope}. Try another spelling, a shorter name, or a broader '
                "query; if still nothing, treat it as unavailable at this shop."
            )
        if session is not None:
            record_alternatives(session, results)
        return json.dumps({"query": query, "ingredient_type": ingredient_type, "candidates": results},
                          ensure_ascii=False)

    @beta_tool
    def build_order_list(items: list[OrderListItem]) -> str:
        """Build the final shopping list with links, prices and total; returns the text to show the user.

        One item per recipe ingredient (merge repeated additions of the same product into one item
        whose ingredient.amount is the summed amount). quantity is the number of catalog units to
        buy, not the recipe amount: e.g. 4.5 kg of malt sold "pr. 100 g" is 45, in 25 kg sacks is 1;
        60 g of a hop sold in 100 g packs is 1. For products with a weight or volume pack_size the
        tool recalculates quantity from ingredient.amount and unit, so keep those accurate; your
        quantity is used as given only for per-pack items such as yeast. product_handle must be a handle returned by search_catalog.
        source: "matched" = the recipe's own product; "substituted" = your substitute, which the
        user accepted; "user_override" = a product the user chose instead; "unavailable" = no
        product (e.g. the user rejected the substitute) - leave product_handle null, the line is
        still listed. substituted and user_override require substitution {original_name, reason,
        confidence}; matched and unavailable must not have one. Show the returned list to the user
        verbatim. Call again to rebuild after any change.

        Args:
            items: One entry per recipe ingredient, with the chosen product and quantity.
        """
        session = _session()
        order = build_order_list_impl(ctx, session, items)
        sections = []
        if notes := quantity_notes(order):
            sections.append(
                "Quantities recalculated from pack sizes (already applied in the list below):\n"
                + "\n".join(f"- {n}" for n in notes)
            )
        if notes := alternative_notes(order, session):
            sections.append(
                "Equally good in-stock alternatives (the list stays as built). After the list, "
                "name them in your reply with origin and price and say the user can switch to any of them:\n"
                + "\n".join(f"- {n}" for n in notes)
            )
        return "\n\n".join([*sections, order.text])

    return [_report_validation_errors(t) for t in (submit_parsed_recipe, search_catalog, build_order_list)]
