# BrewChat

A chat assistant that takes a pasted beer recipe, matches its ingredients against a real homebrew supplier's live catalog, suggests substitutions for anything unmatched or out of stock, and returns a priced shopping list.

This is a side project and skill demo: an end-to-end product exercise from problem framing through an agentic architecture, built around Anthropic's agentic SDLC (intent -> spec -> build).

## Why

Homebrewers find recipes online in inconsistent formats, then have to manually cross-reference every ingredient against what their supplier actually has in stock, and hunt for substitutes themselves when something's missing. This project explores whether a chat agent can close that gap, matching a recipe straight to a real, purchasable shopping list.

## Project docs

- [`intent.md`](./intent.md): the problem, the proposed outcome, constraints, and open questions, the upstream artifact for this project.
- [`spec.md`](./spec.md): requirements, architecture, and data schemas for the MVP, produced from `intent.md`.

## Approach

BrewChat is designed as a single Claude agent in a tool-use loop rather than a multi-stage pipeline: the agent parses a recipe into a standardized schema, searches a cached copy of the supplier's catalog, and reasons about substitutions using its own brewing knowledge instead of a hand-built rule table, verifying every suggestion against real stock before offering it. See `spec.md` for the full architecture.

## Status

Early build, developed against a real (but unpublished) supplier API. Raw catalog exports are excluded from this repo since no data-use agreement is in place yet with the supplier.
