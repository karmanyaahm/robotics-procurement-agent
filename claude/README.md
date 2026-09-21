# procurement-agent

An acquisition companion for robotics parts and tools, built to run inside a
coding agent (Claude Code or Codex). The agent does the research, browsing and
spec checking. A small CLI, `procure`, does everything that has to be exact:
pricing math, ranking, and deciding whether a verification actually matched.

The split is deliberate. In benchmarks, agents' weakest tasks are "cheapest
offer" and "compatible product", and their typical miss is an item that
violates one requirement. So here:

- Structured sources (distributor APIs) come first; the browser is a fallback.
- Every requirement needs an explicit, evidenced pass for the exact SKU.
- Every number in a recommendation traces to a record with a URL and timestamp.
- A second, blind pass re-checks the top offers before anything is recommended.

## Setup

```bash
uv sync                              # or: pip install -e '.[dev]'
cp .env.example .env                 # add API keys (optional, see below)
uv run --group dev pytest -q         # 42 tests
```

1. **Approved vendors.** Edit `config/vendors.yaml`. Everything ships as
   `approved: false`, and `procure compare` refuses to run until you approve
   at least one vendor. Set `login: true` for vendors where your account gets
   different pricing, `cart_ok: true` where the agent may add to cart to read
   shipping, and a `shipping` rule if you know the vendor's terms.
2. **Policy.** Edit `config/policy.yaml`: sales-tax rate, how old a capture can
   get before it's flagged stale, and the default lead-time limit.
3. **API keys** (optional but recommended for electronics):
   - DigiKey: create a *production* app at developer.digikey.com and put the
     client ID/secret in `.env`. Only DigiKey's standard pricing comes back;
     your account pricing has to come from the logged-in site.
   - Mouser: get a free Search API key at mouser.com/api-search.
4. **Browser**, for vendors without an API:
   - Claude Code: install the Claude in Chrome extension and launch with
     `claude --chrome` when you need logged-in vendor pages. Use a dedicated
     Chrome profile that is signed in to vendor accounts only, never email or
     banking.
   - Headless public pages (Claude Code or Codex):
     `npm install -g @playwright/cli@latest`, then optionally
     `playwright-cli install --skills`.

## Use it

From the repo root, in Claude Code (`claude`, or `claude --chrome`) or Codex:

> Find a USB Wi-Fi adapter that works with the in-kernel drivers on Ubuntu
> 24.04's kernel and our Jetson's kernel, qty 3, cheapest from approved vendors.

> Price a rolling tool cabinet under 27" wide with a keyed lock. Compare the
> 2-drawer vs 3-drawer and tell me the landed-cost difference.

The agent follows `AGENTS.md`. It creates `requests/<date>-<slug>/`, agrees on
testable requirements with you, researches, captures offers, runs `compare`,
verifies the top offers, and writes `report.md`.

- **Claude Code** hands verification to the `offer-verifier` subagent, which
  works in its own context.
- **Codex** reads `AGENTS.md` natively. For verification, start a *fresh*
  session and give it only the `verify-list` output, so it can't anchor on
  the capture notes.

See the output format without any setup:

```bash
uv run procure compare examples/toolbox --config examples/toolbox/config
```

(Fictional vendors and prices. It shows a verified winner, a 2- vs 3-drawer
delta, a spec failure, an unapproved vendor, and a buy-extra price-break flag.)

## Layout

```
AGENTS.md                    rules + workflow (Codex reads this; CLAUDE.md imports it)
CLAUDE.md                    Claude Code specifics (browser, subagent)
.claude/agents/offer-verifier.md
config/vendors.yaml          approved vendors, shipping/tax/cart rules
config/policy.yaml           tax rate, staleness, lead-time limits
requests/_template/          copied by `procure new`
requests/<date>-<slug>/
  request.yaml               items, qty, testable requirements
  research.md                compatibility findings with sources
  offers.jsonl               append-only captures (newest per id wins)
  verifications.jsonl        blind re-checks
  comparison.md              written by `procure compare`
  report.md                  written by the agent
  evidence/                  screenshots
procure/                     the CLI (models, pricing, compare, sources/)
examples/toolbox/            worked example with its own config
tests/
```

## How `compare` decides

For each item it takes the newest capture of each offer and excludes any offer
that:

- is from an unapproved vendor or a different currency;
- is missing an explicit pass on any requirement;
- can't ship the quantity from stock within the lead-time limit (tightened by
  `need_by`);
- or failed verification.

It then prices the rest: the cheapest orderable quantity given MOQ, order
multiple and price breaks, including buying extra when a higher break is
cheaper overall. Next it adds fees, shipping (a cart quote, else the vendor
rule, else unknown and shown as a lower bound) and tax. Offers are ranked by
landed cost. With `compare_options: true` it also reports each option's best
landed cost and the delta to the cheapest option.

## Limitations

- The DigiKey and Mouser clients were built from published request/response
  formats and tested against saved fixtures, not live keys. On first use, run
  a command with `--raw` to check the response shape.
- The APIs return list pricing and no shipping. Account pricing and cart
  shipping need the browser.
- Mouser price strings are parsed assuming a `.` decimal separator (US locale).
- Buy-extra optimization looks at price breaks only, not free-shipping
  thresholds.
- There's no McMaster-Carr or Amazon integration. Those go through the
  browser.
