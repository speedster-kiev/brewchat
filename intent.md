# Intent: BrewChat recipe-to-basket demo
Author: Maks. Status: draft (revised after review).

## Problem
Homebrewers find recipes online (Brewer's Friend, forums, blogs) in inconsistent formats, then have to manually cross-reference every ingredient against what their supplier actually has in stock. When something is missing, they are stuck hunting for a substitute themselves.

Evidence: my own experience. Ordering the right ingredients and finding substitutes takes me around 2 hours per brew. At 10-12 brews a year that is roughly a full day per year spent on ordering alone. I assume I'm not alone in this, but that assumption is not yet validated with other brewers or with the supplier. Most customers of the supplier already order online and pick up in the shop, so the ordering step is where the time goes, not the shopping trip.

## Goals, in order of importance
1. Portfolio showcase: an end-to-end product exercise (intent -> spec -> agentic build) that demonstrates how I frame a problem and ship an AI agent against real data.
2. Save my own time when ordering homebrew supplies.
3. Potential extension and collaboration: with the supplier used in this demo, and later with other suppliers or other shops running on the same third-party online shop platform.

When these goals conflict, the higher one wins. In practice: clean architecture and clear documentation take priority over demo polish, and demo polish takes priority over supplier-specific customization.

## Proposed outcome
A web chat assistant that takes a pasted beer recipe, matches its ingredients against a daily-cached copy of one supplier's catalog, suggests a substitution for anything unmatched or out of stock, and returns a priced, ready-to-use shopping list.

Substitutions are reasoned from the assistant's own brewing knowledge, not a rule table. They are always presented as suggestions with a one-line reason, and the assistant must say explicitly when it isn't confident in a swap. There is no human-in-the-loop check on substitutions; that would break the flow and nobody would wait for it. Instead every suggested substitution, and which one the user actually kept, is logged so the quality of the suggestions can be reviewed afterwards, manually or with AI assistance.

The assistant stays scoped to beer recipes and brewing topics and declines or redirects anything else.

The shopping list is the end of the flow for this version, since there is no cart or order endpoint available (see Constraints). The list must be easy to carry into the supplier's shop: copyable, with a working product link per line, so the user is not left with a dead end.

## Success criteria
- Primary: measured time to place an order for a brew, using the chatbot versus my current manual process. Baseline is about 2 hours. The target is a clear, repeatable reduction that I can show with numbers.
- Secondary: the supplier is interested in a one-month pilot with the chat live on his website.
- Stop criterion: if after a live walkthrough the supplier is not interested in a pilot, goals 1 and 2 still stand and the project continues as a portfolio piece and personal tool. Goal 3 is then parked, not pursued further with this supplier.

## Affected users and systems
- Homebrewers (end users) who paste a recipe and receive a shopping list. Initially just me.
- HopCellar (anonymized name), the one supplier used for this demo. His shop runs on a third-party online shop platform; the product data used here comes from that platform's unauthenticated product JSON endpoints (`/json/products/all`, `/json/products/id/{id}`). Supplier identity and endpoint details are to be kept out of the codebase and docs, in a settings/config file (detail in spec.md).
- The supplier's benefit, as I see it: shorter time-to-order for his customers, substitutions as a lever to steer demand toward stock he has or wants to move (upsell or cross-sell in the same flow), and a natural place to surface current deals or promotions inside the chat. None of this is discussed with him yet.
- Other suppliers or shops on the same platform, if the integration turns out to be platform-level rather than supplier-level (goal 3, future).

## Constraints
- No agreement, and no initial discussion yet, with the supplier. This is a personal project built on publicly reachable data.
- The supplier's shop runs on a third-party online shop platform. Direct integration with the supplier is not possible; any real integration needs an agreement with the platform provider, and none exists.
- The product data is read-only (catalog, stock, pricing) and is scraped via the platform's unauthenticated JSON endpoints. There is no cart or order-creation endpoint, so this version stops at a generated shopping list, not a purchase.
- Until data-use terms are agreed with the supplier and/or the platform, the demo stays private: access-gated, shared only with the supplier and people I choose, no public URL. Raw catalog exports stay out of the repo.
- The supplier is anonymized in all code, docs and logs. Real name, domain and endpoint base URL live only in a local settings file that is not committed.
- Single supplier only, no multi-supplier comparison.
- Stateless per session: no user accounts, saved recipes or order history. Substitution logging is the one exception and stores no user identity.
- Substitution only, no recipe engineering: no scaling, batch size or water chemistry adjustments.
- Web chat only, no mobile app.

## Risks
- A confidently wrong substitution (e.g. a bittering hop offered as an aroma swap, or the wrong yeast family) makes the supplier's shop look bad, and the customer blames the shop, not the bot. Mitigated by reasons-plus-confidence in every suggestion and by the substitution log.
- Stale stock: the catalog is cached daily, so a product can be shown as in stock after it sold out the same morning. Accepted for this version since the catalog changes infrequently; the list must state the cache timestamp.
- Data-use: using scraped stock and prices in a demo without agreement could sour the supplier conversation before it starts. Mitigated by keeping the demo private and anonymized until there is a conversation.
- Dead end at checkout: without a cart endpoint the user still has to add items in the shop by hand. Mitigated by per-line product links and a copyable list; a real fix needs the platform.

## Timeline
No hard deadline. Suggested phasing: 2-3 weeks to a working end-to-end demo against the cached catalog, first used for my own next brew to get a baseline-versus-chatbot time measurement, then a live walkthrough with the supplier as the opening of the pilot conversation.

## Open questions
- What is the right way to measure "time to order placed" so that the before/after comparison is fair (same recipe, same cart size, timed from recipe in hand to order confirmed)?
- Does the supplier want to receive orders generated by the tool as a formatted list, an email, or something else?
- Are prices in DKK, and should totals shown to the user be VAT-inclusive or exclusive?
- Would the supplier want to see or approve substitution behaviour for his catalog before customers do, or is the substitution log enough for him?
- Is the supplier willing to link the chat from his site for a one-month pilot? (This is effectively the secondary success criterion.)
- What would a monetization or collaboration model look like if a pilot happens (affiliate commission, referral fee, paid access, or a platform-level offering)?
- Are there legal or data-use terms attached to the platform's product endpoints and the supplier's catalog data?
- Does the working brewing-ingredient category filter match the supplier's own sense of what counts as an ingredient, especially edge cases like all-grain kits or cask/specialty beer?
