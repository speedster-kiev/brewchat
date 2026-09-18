"""Live-model evals for BrewChat: recipe parse accuracy and adversarial scoping.

Run by hand; every run calls the Anthropic API and costs money (evals/README.md).

    uv run python scripts/run_evals.py parse [--recipe N | --only recipe_N]
    uv run python scripts/run_evals.py adversarial [--only C4] [--judge]

Both modes run against the SYNTHETIC catalog (tests/fixtures/catalog_sample.json)
with the committed example config, in a temp directory, so they never need the
real supplier config and never write to the real logs. Every run is written to
evals/results/<UTC timestamp>-<mode>.jsonl. Fixture parsing and scoring live in
brewchat/evals.py (unit-tested without the API).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brewchat import evals

# USD per million tokens (input, output), for the cost estimate printed after a
# run. Cache writes bill at 1.25x input (5-minute TTL), cache reads at 0.1x.
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
}

STUB_REPLY = (
    "stubbed in parse eval: no catalog lookup or list building happens in this run."
)


# ---------------------------------------------------------------------------
# Setup: synthetic catalog, temp logs, tools
# ---------------------------------------------------------------------------


def build_context(tmp: Path):
    """ToolContext over the synthetic catalog, with cache and logs inside ``tmp``."""
    from brewchat.agent.tools import ToolContext
    from brewchat.catalog.sync import filter_and_project, write_cache
    from brewchat.config import EXAMPLE_CONFIG, load_settings

    base = load_settings(EXAMPLE_CONFIG)
    settings = dataclasses.replace(
        base, cache_path=tmp / "data" / "catalog.sqlite", logs_dir=tmp / "logs"
    )
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    raw = json.loads(evals.SYNTHETIC_CATALOG.read_text(encoding="utf-8"))
    products = filter_and_project(raw, settings)
    write_cache(products, settings.cache_path, datetime.now(UTC))
    ctx = ToolContext.from_settings(settings)
    sold_out = {p.handle.strip("/") for p in products if not p.in_stock()}
    return ctx, products, sold_out


def stub_like(real_tool: Any) -> Any:
    """A no-op tool with exactly the real tool's name, description and input schema."""
    from anthropic import beta_tool

    def _stub(**_kwargs: Any) -> str:
        return STUB_REPLY

    return beta_tool(
        _stub,
        name=real_tool.name,
        description=real_tool.description,
        input_schema=real_tool.input_schema,
    )


def parse_mode_tools(ctx: Any) -> list:
    """Real submit_parsed_recipe; search_catalog and build_order_list stubbed to no-ops."""
    from brewchat.agent.tools import make_tools

    real = {t.name: t for t in make_tools(ctx)}
    return [
        real["submit_parsed_recipe"],
        stub_like(real["search_catalog"]),
        stub_like(real["build_order_list"]),
    ]


def make_runner(ctx: Any, model: str, tools: list | None = None):
    from brewchat.agent.runner import Runner

    return Runner(ctx, tools=tools, model=model)


def turn_record(message: str, result: Any) -> dict[str, Any]:
    order = result.order_list
    if order is not None and hasattr(order, "model_dump"):
        order = order.model_dump(mode="json")
    return {
        "message": message,
        "reply": result.reply,
        "tool_calls": [
            {"name": c["name"], "input": c.get("input")} for c in result.tool_calls
        ],
        "order_list": order,
        "usage": result.usage,
        "stopped_after": result.stopped_after,
    }


def sum_usage(usages: list[dict[str, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for u in usages:
        for k, v in u.items():
            total[k] = total.get(k, 0) + v
    return total


def estimate_cost(model: str, usage: dict[str, int]) -> float | None:
    """USD estimate from summed usage, or None for a model not in PRICES."""
    price = next((p for prefix, p in PRICES.items() if model.startswith(prefix)), None)
    if price is None:
        return None
    inp, out = price
    return (
        usage.get("input_tokens", 0) * inp
        + usage.get("cache_creation_input_tokens", 0) * inp * 1.25
        + usage.get("cache_read_input_tokens", 0) * inp * 0.1
        + usage.get("output_tokens", 0) * out
    ) / 1_000_000


def print_usage(model: str, usage: dict[str, int]) -> None:
    cost = estimate_cost(model, usage)
    print(
        f"usage ({model}): {usage.get('api_calls', 0)} API calls, "
        f"input {usage.get('input_tokens', 0):,} uncached + "
        f"{usage.get('cache_creation_input_tokens', 0):,} cache write + "
        f"{usage.get('cache_read_input_tokens', 0):,} cache read, "
        f"output {usage.get('output_tokens', 0):,} (incl. thinking)"
        + (f"; estimated cost ${cost:.2f}" if cost is not None else "")
    )


class ResultsWriter:
    def __init__(self, mode: str, results_dir: Path) -> None:
        results_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.path = results_dir / f"{stamp}-{mode}.jsonl"
        self.mode = mode

    def write(self, record: dict[str, Any]) -> None:
        record = {
            "mode": self.mode,
            "written_at": datetime.now(UTC).isoformat(),
            **record,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _fmt_pct(x: float) -> str:
    return f"{100 * x:5.1f}%"


# ---------------------------------------------------------------------------
# parse mode
# ---------------------------------------------------------------------------


def run_parse(args: argparse.Namespace) -> int:
    from brewchat.agent.session import Session

    recipes = evals.load_recipes()
    expected = evals.load_expected()
    wanted = sorted(recipes)
    if args.recipe is not None:
        wanted = [args.recipe]
    elif args.only:
        wanted = [int(args.only.lower().removeprefix("recipe_").removeprefix("recipe"))]
    writer = ResultsWriter("parse", args.results_dir)
    scores: list[evals.RecipeScore] = []
    usages: list[dict[str, int]] = []

    with tempfile.TemporaryDirectory(prefix="brewchat-eval-") as tmp:
        ctx, _products, _sold_out = build_context(Path(tmp))
        runner = make_runner(ctx, args.model, tools=parse_mode_tools(ctx))
        for n in wanted:
            rid = f"recipe_{n}"
            if n not in recipes or rid not in expected:
                print(f"{rid}: no such recipe / no expected entry", file=sys.stderr)
                return 2
            session = Session()
            started = time.monotonic()
            error = None
            try:
                # Only the parse is scored: end the turn once it's submitted
                # instead of paying for stubbed searches and a closing reply.
                result = runner.run_turn(
                    session, recipes[n], stop_after={"submit_parsed_recipe"}
                )
                turn = turn_record(recipes[n], result)
            except Exception as exc:  # noqa: BLE001 - record the failure and keep going
                error = f"{type(exc).__name__}: {exc}"
                turn = {
                    "message": recipes[n],
                    "reply": "",
                    "tool_calls": [],
                    "order_list": None,
                    "usage": {},
                }
            usages.append(turn["usage"])
            parsed = evals.extract_parsed_ingredients(turn["tool_calls"])
            if parsed is None and session.parsed_recipe:
                parsed = [i.model_dump() for i in session.parsed_recipe]
            score = evals.score_recipe(rid, expected[rid], parsed or [])
            scores.append(score)
            notes = []
            if error:
                notes.append(error)
            if parsed is None:
                notes.append("submit_parsed_recipe was never called")
            writer.write(
                {
                    "fixture_id": rid,
                    "turns": [turn],
                    "parsed": parsed,
                    "score": score.to_dict(),
                    "verdict": "pass"
                    if score.accuracy >= evals.PARSE_PASS_BAR
                    else "fail",
                    "notes": notes,
                    "model": args.model,
                    "seconds": round(time.monotonic() - started, 1),
                }
            )
            print(f"  {rid}: {_fmt_pct(score.accuracy)}", file=sys.stderr)

    summary = evals.summarize(scores)
    print()
    print(
        f"{'recipe':<11}{'acc':>7}{'type':>7}{'name':>7}{'amount':>8}{'unit':>7}{'miss':>6}{'extra':>7}"
    )
    for s in scores:
        fa = {
            f: (s.field_correct[f] / s.field_total[f] if s.field_total[f] else 0.0)
            for f in evals.SCORED_FIELDS
        }
        print(
            f"{s.recipe_id:<11}{_fmt_pct(s.accuracy):>7}{_fmt_pct(fa['type']):>7}{_fmt_pct(fa['name']):>7}"
            f"{_fmt_pct(fa['amount']):>8}{_fmt_pct(fa['unit']):>7}{len(s.missing):>6}{len(s.extras):>7}"
        )
    print("-" * 60)
    fa = summary.field_accuracy
    print(
        f"{'overall':<11}{_fmt_pct(summary.accuracy):>7}{_fmt_pct(fa['type']):>7}{_fmt_pct(fa['name']):>7}"
        f"{_fmt_pct(fa['amount']):>8}{_fmt_pct(fa['unit']):>7}"
    )
    print(
        f"timing agreement (reported, not scored): {_fmt_pct(summary.timing_agreement)}"
    )
    for s in scores:
        for m in s.missing:
            print(f"  {s.recipe_id} missing: {m}")
        for x in s.extras:
            print(f"  {s.recipe_id} extra:   {x}")
        for mm in s.mismatches:
            print(
                f"  {s.recipe_id} wrong {','.join(mm['wrong_fields'])}: expected {mm['expected']} got {mm['actual']}"
            )
    bar = evals.PARSE_PASS_BAR
    print()
    print_usage(args.model, sum_usage(usages))
    print(
        f"\n{'PASS' if summary.passed else 'FAIL'}: {summary.correct}/{summary.total} fields "
        f"({_fmt_pct(summary.accuracy).strip()}, bar {bar:.0%}). Results: {writer.path}"
    )
    return 0 if summary.passed else 1


# ---------------------------------------------------------------------------
# adversarial mode
# ---------------------------------------------------------------------------


def judge(
    client: Any, model: str, fixture: evals.Fixture, turns: list[dict[str, Any]]
) -> tuple[bool, str, dict[str, int]]:
    """One API call per fixture: grade the transcript against the fixture's Expected text."""
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        output_config={"format": {"type": "json_schema", "schema": evals.JUDGE_SCHEMA}},
        messages=[{"role": "user", "content": evals.judge_prompt(fixture, turns)}],
    )
    text = "".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    )
    from brewchat.agent.runner import add_usage

    usage: dict[str, int] = {}
    add_usage(usage, response.usage)
    return (*evals.parse_judge_verdict(text), usage)


def run_adversarial(args: argparse.Namespace) -> int:
    from brewchat.agent.session import Session

    fixtures = evals.load_adversarial()
    if args.only:
        wanted = {x.strip().upper() for x in args.only.split(",")}
        fixtures = [f for f in fixtures if f.id in wanted]
        if not fixtures:
            print(f"no fixture matches --only {args.only}", file=sys.stderr)
            return 2
    writer = ResultsWriter("adversarial", args.results_dir)
    rows: list[tuple[evals.Fixture, evals.Verdict, str]] = []
    usages: list[dict[str, int]] = []
    judge_client = None
    if args.judge:
        import anthropic

        judge_client = anthropic.Anthropic()

    with tempfile.TemporaryDirectory(prefix="brewchat-eval-") as tmp:
        ctx, _products, sold_out = build_context(Path(tmp))
        runner = make_runner(ctx, args.model)  # real tools, synthetic catalog
        for fx in fixtures:
            session = (
                Session()
            )  # each fixture (and each B sequence) is its own fresh session
            turns: list[dict[str, Any]] = []
            error = None
            try:
                for msg in fx.user_messages:
                    turns.append(turn_record(msg, runner.run_turn(session, msg)))
                    usages.append(turns[-1]["usage"])
            except Exception as exc:  # noqa: BLE001 - record the failure and keep going
                error = f"{type(exc).__name__}: {exc}"
            if error:
                verdict = evals.Verdict(
                    False, [evals.Check("ran", False, error)], review=True
                )
            else:
                verdict = evals.evaluate_fixture(fx, turns, sold_out_handles=sold_out)
            judge_note = ""
            judge_result = None
            if judge_client is not None and not error:
                try:
                    ok, reason, judge_usage = judge(judge_client, args.model, fx, turns)
                    usages.append(judge_usage)
                except Exception as exc:  # noqa: BLE001 - record the failure and keep going
                    ok, reason = False, f"judge error: {type(exc).__name__}: {exc}"
                judge_result = {"passed": ok, "reason": reason}
                verdict.checks.append(evals.Check("judge", ok, reason))
                verdict.passed = verdict.passed and ok
                judge_note = reason
            writer.write(
                {
                    "fixture_id": fx.id,
                    "category": fx.category,
                    "title": fx.title,
                    "expected": fx.expected,
                    "turns": turns,
                    "verdict": "pass" if verdict.passed else "fail",
                    "checks": verdict.to_dict()["checks"],
                    "review": verdict.review,
                    "notes": verdict.notes + ([error] if error else []),
                    "judge": judge_result,
                    "model": args.model,
                }
            )
            rows.append((fx, verdict, judge_note))
            print(f"  {fx.id}: {'pass' if verdict.passed else 'FAIL'}", file=sys.stderr)

    print()
    print(f"{'id':<5}{'cat':<5}{'result':<8}{'review':<8}checks")
    for fx, v, _ in rows:
        failed = [
            f"{c.name}({c.note})" if c.note else c.name for c in v.checks if not c.ok
        ]
        info = "; ".join(failed) if failed else "ok"
        print(
            f"{fx.id:<5}{fx.category:<5}{'pass' if v.passed else 'FAIL':<8}{'yes' if v.review else '':<8}{info}"
        )
    print("-" * 60)
    all_ok = True
    for cat in "ABCD":
        cat_rows = [v for fx, v, _ in rows if fx.category == cat]
        if not cat_rows:
            continue
        n_ok = sum(v.passed for v in cat_rows)
        all_ok &= n_ok == len(cat_rows)
        print(f"{cat}: {n_ok}/{len(cat_rows)}")
    print()
    print_usage(args.model, sum_usage(usages))
    print(
        f"\n{'PASS' if all_ok else 'FAIL'} (bar: 100% on A-D). Replies for human review: {writer.path}"
    )
    return 0 if all_ok else 1


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="BrewChat live-model evals (costs API calls; needs ANTHROPIC_API_KEY). "
        "See evals/README.md.",
    )
    p.add_argument(
        "mode",
        choices=["parse", "adversarial"],
        help="parse: recipe parse accuracy vs recipes.expected.json (bar 90%%); "
        "adversarial: A-D scoping fixtures (bar 100%%)",
    )
    p.add_argument(
        "--model",
        default=None,
        help="model id (default: $BREWCHAT_MODEL from .env or the environment, else claude-haiku-4-5)",
    )
    p.add_argument(
        "--only",
        help="run one fixture: recipe_N / N for parse, e.g. C4 or A1,B2 for adversarial",
    )
    p.add_argument("--recipe", type=int, help="parse mode: run only recipe N")
    p.add_argument(
        "--judge",
        action="store_true",
        help="adversarial mode: also grade each transcript against its Expected text with the "
        "model (one extra API call per fixture); the judge must agree for a pass",
    )
    p.add_argument(
        "--results-dir",
        type=Path,
        default=evals.RESULTS_DIR,
        help="where the JSONL run record goes (default evals/results/)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from brewchat.config import load_env_file

    load_env_file()
    if args.model is None:
        from brewchat.config import DEFAULT_MODEL

        args.model = os.environ.get("BREWCHAT_MODEL") or DEFAULT_MODEL
    if not (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        print(
            "ANTHROPIC_API_KEY is not set. These evals call the live Anthropic API; add it to .env "
            "(see .env.example) or export it, and re-run. See evals/README.md for the expected cost.",
            file=sys.stderr,
        )
        return 2
    if args.judge and args.mode != "adversarial":
        print("--judge only applies to adversarial mode", file=sys.stderr)
        return 2
    return run_parse(args) if args.mode == "parse" else run_adversarial(args)


if __name__ == "__main__":
    sys.exit(main())
