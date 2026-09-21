@AGENTS.md

## Claude Code specifics

**Browser.** Start with `claude --chrome` only when the request needs a
logged-in vendor (`login: true` in `vendors.yaml`) or a page that plain fetch
and `playwright-cli` can't handle. That mode drives the Claude in Chrome
extension and shares your Chrome login state. Leaving it on by default loads
the browser tools into every session and costs context. Run `/chrome` to
check the connection. Site permissions live in the extension's settings; the
user should use a dedicated Chrome profile that is signed in to vendor
accounts only.

**Verification.** Step 5 goes to the `offer-verifier` subagent
(`.claude/agents/offer-verifier.md`). Pass it the full JSON from
`uv run procure verify-list <dir>` and the request directory. It works in a
separate context, so it doesn't inherit your capture notes. That separation is
the point: don't paste your captured prices into the handoff.

**Parallel capture.** For requests with many vendors, you may use subagents to
capture offers per vendor, but every result must land in `offers.jsonl` as a
record. Summaries in chat don't count.
