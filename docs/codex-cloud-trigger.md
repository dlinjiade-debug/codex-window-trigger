# Direct Codex Cloud trigger

This is the active delivery design. GitHub Actions creates a deterministic
same-repository PR and then posts one exact, non-review `@codex` comment. The
resulting Codex Cloud chat is the best-effort usage touch. It does not prove the
five-hour reset time moved, and it may use an existing flexible-credit balance
if included usage is unavailable.

## One-time setup

1. In the Codex GitHub connector, authorize exactly
   `dlinjiade-debug/codex-window-trigger` in addition to any access the user
   deliberately keeps for other repositories.
2. Create one Codex Cloud environment for that exact repository. Add no secrets
   or environment variables, leave internet access off, and do not add setup
   commands unless Codex requires a harmless default to save the environment.
3. Do not enable automatic code review. Reviews use a separate delivery mode
   and quota and are not this general-usage touch.
4. Leave repository variable `SENTINEL_ENABLED` absent or `false` until the
   controlled canary passes.

The workflow comment begins with `@codex`, supplies only a validated episode
key and the trusted PR creation time, tells Codex not to inspect files, run
commands, modify the repository, create commits, or post follow-ups, and asks
for the fixed short receipt before stopping. The publisher accepts only the
exact comment authored by `github-actions[bot]`. It queries all comment pages,
validates the authenticated comment-create response, and rechecks an uncertain
response. A successful receipt uses the comment's trusted `created_at` as the
24-hour touch time; the PR time remains provenance in the comment body.

Recovery first classifies every trusted PR without posting. If one PR lacks its
exact comment, any other confirmed comment or persisted trigger inside the
24-hour cooldown defers the repair; two missing comments fail closed. This keeps
a recovery run from starting a second Cloud chat during the cooldown.

Before its sole POST, the publisher commits a bounded `comment_attempts` marker.
This marker is not a successful trigger receipt. If the POST response is lost
and the exact comment is not yet listed, later runs remain read-only instead of
posting again. Once the exact comment appears, reconciliation removes the marker.
If it never appears, inspect the PR and clear the marker manually only after
confirming that retrying cannot duplicate a Cloud touch.

## Controlled canary

The only canary identity is:

- title: `[codex-5h-touch] canary-000000000000`
- branch: `trigger/000000000000`
- file: `triggers/000000000000.json`
- workflow inputs: `operation=canary`, `dry_run=false`,
  `canary_approved=true`

Before dispatch, confirm baseline is initialized, auto-reload remains off, and
the user has approved this one live run knowing it can consume shared five-hour
usage or existing credits. Then verify all of the following:

- one PR and one exact bot comment exist; retries do not duplicate either;
- exactly one Codex Cloud chat appears;
- the chat returns only the short receipt and performs no tools, file changes,
  commits, reviews, or follow-ups;
- account usage/reset indicators are observed before and after, but any change
  is reported as an empirical canary result rather than a guarantee.

If a bot-authored `@codex` mention is ignored, stop with the schedule disabled.
Do not substitute a PAT, GitHub App private key, OpenAI key, email credential,
automatic review, or local polling task without a separately approved design.

Because both the probe and touch run on GitHub/Codex infrastructure, the user's
computer may be off and the phone does not need a VPN after setup. GitHub's
five-minute cron can be delayed and is not a real-time SLA.

Only after the canary is accepted should `SENTINEL_ENABLED=true` be set. To
pause or retire the system, set it back to `false` (or disable the workflow)
before changing credits, closing PRs, or disconnecting the Codex environment.
