# Codex window trigger

Deployment is **disabled** until manually approved setup and a controlled canary
succeed. A five-minute GitHub Actions probe reads public reset predictions and
creates one bot PR for a new eligible episode, then posts exactly one
non-review `@codex` PR comment. The resulting Codex Cloud chat is itself the
single best-effort five-hour-window touch.

This project cannot read a personal account's live five-hour balance. If included usage is already exhausted, the cloud touch may use an existing flexible-credit balance. Keep auto-reload off and pause the sentinel before purchasing credits. The touch is best-effort and does not prove that the five-hour timer moved.

No promise of two full five-hour windows or zero credit use is possible. Before
enablement, the user must confirm auto-reload is off and there is no existing
credit balance they need to preserve from this task. Keep those account checks,
screenshots, and execution ledgers private and outside this repository.

## Safety model

Eligibility requires score ≥30%, fresh and consistent public forecast/feed/
timeline, a clear broad Codex/Work reset, a future target within five hours if
supplied, an unhandled stable episode key, and 24 hours since any successful
trigger (including canary). First valid observation only establishes baseline.
Invalid/stale data fails closed. State detail records are bounded to 100;
handled identities remain durable. All open and closed PR pages are inspected,
but only same-repository `github-actions[bot]` PRs are candidates. Before a
new candidate is acknowledged, an exact same-bot `@codex` comment must also be
observable. Outsider and near-match comments never satisfy idempotency.

Only code, synthetic tests, public signal metadata, trigger records and dedup
state belong in this dedicated **public** repository. Never upload accounts,
login data, cookies, personal emails, secrets, unrelated projects, `.runtime`,
or `.superpowers` execution ledgers. Inspect `git diff --cached` before any
manual commit/push; never force-add ignored files.

The workflow uses the repository-scoped `GITHUB_TOKEN` only, with
`contents: write`, `issues: write`, and `pull-requests: write` (all other
permissions none). Standard `ubuntu-latest` runners only. Release commits are
pinned: checkout v7.0.1 and
setup-python v7.0.0, verified 2026-08-31. No external Python dependencies.

## Setup (manual, schedule stays OFF)

1. Review source and staged files. Provision a dedicated public repository and
   set its default branch to `main`. Do not reuse a private/account-data repo.
   Leave `SENTINEL_ENABLED` unset or `false`; disabled scheduled jobs allocate
   no runner. No keepalive commits are created.
2. Allow GitHub Actions to create pull requests in this repository's settings.
   Authorize the Codex GitHub connector for this repository only. Create one
   minimal Codex Cloud environment for the exact repository, with no secrets or
   environment variables and internet access off. Do not enable automatic code
   review. Follow the [direct cloud-trigger instructions](docs/codex-cloud-trigger.md).
3. Run local tests and schema validation below. Manually dispatch the sentinel
   with `operation=poll`, `dry_run=true` (the default). Dry-run never mutates
   Git, PRs, or stored state, regardless of operation/approval/enablement.
4. Establish valid first-run state with manual `operation=baseline`,
   `dry_run=false`. It can only initialize (or no-op if already initialized),
   never create a PR. It does not require enabling the schedule.
5. Reconfirm credit safeguards. Obtain explicit user approval for this specific
   one-time canary at action time. Follow [the controlled canary procedure](docs/codex-cloud-trigger.md)
   with `operation=canary`, `dry_run=false`, `canary_approved=true`. Baseline is
   required. Approval defaults false and never authorizes normal polling.
6. Verify exactly one PR comment, one Cloud chat, the single short receipt, no
   extra work/tools/changes, and offline cloud operation. If Codex ignores a
   bot-authored mention, deployment is blocked;
   do not bypass with new credentials. Leave the canary PR open.
7. Only after acceptance, set repository variable `SENTINEL_ENABLED=true` for
   live scheduled polling. Manual live `poll` also requires this variable.

| Operation | Dry-run | Live prerequisites | Possible persistent change |
| --- | --- | --- | --- |
| Any | true | None of the live gates | None |
| poll | false | schedule/manual; enabled | State and at most one eligible PR/comment pair |
| baseline | false | manual dispatch | First initialization only |
| canary | false | manual; initialized; approval now | One fixed canary PR/comment pair and state |

The publisher independently enforces these gates, public visibility, repository
identity, every effective origin push/fetch URL, clean tracked files, and the
checked-out default branch. A caller-selected ref is not accepted. Git operations
use argument arrays and PR text is passed literally through a body file.

## Local validation and dry-run

Python 3.13+ and Git are sufficient for tests (GitHub calls are narrowly faked;
Git checkouts and bare remotes are real and temporary):

```console
python -m unittest discover -s tests -v
python -m compileall -q codex_window_trigger scripts tests
git diff --check
actionlint -shellcheck= .github/workflows/ci.yml .github/workflows/sentinel.yml
python -m codex_window_trigger.main --dry-run --now 2026-08-31T01:00:00Z --forecast tests/fixtures/forecast.json --feed tests/fixtures/feed.json --timeline tests/fixtures/timeline.json
```

The workflow schema test runs actionlint when installed (or when `ACTIONLINT`
points to it); otherwise it skips. Run actionlint explicitly before deployment.
CI runs on code changes, not trigger/state-only changes. Live publisher operation
uses `python -m scripts.publish` from the default-branch checkout and the workflow
environment; local CLI planning by itself never publishes anything.

## Transactions, retries, and logs

The CLI stages only ignored `.runtime` files. The publisher queries all PR pages,
pushes one deterministic `trigger/<12-hex>` branch, creates a literal-title PR,
and posts a JSON-file-backed non-review `@codex` issue comment. Only after the
trusted PR and exact bot-authored comment are both observable does it reconcile
stored state against the PR's actual creation timestamp and commit state to the
default branch. It never persists a trigger candidate as proof of success. PRs
stay open.

A failed PR create leaves an immutable trigger commit to reuse. A later poll
reuses the original payload, even if the new poll has a different receipt time;
unknown branch content is refused, never overwritten or force-pushed. Uncertain
push, PR, and comment results are re-queried across all pages. A retry completes
one missing exact comment without creating a second PR/comment, but first applies
the 24-hour cooldown from persisted state and every other confirmed comment. A
recent confirmed touch defers that repair; more than one unacknowledged PR fails
closed before any cloud touch. A failed state push is recovered from the
observable PR/comment pair on the next fresh checkout.
Do not manually edit/rewrite trigger branches or automatically close them.

Actions logs report the publisher outcome and a short public decision containing
observation time, score, key and reason (including freshness rejection). The same
decision is in ignored `.runtime/decision.json` for local inspection. No raw API
responses, account information, environment values or subprocess stderr are logged.
Do not upload runtime logs/artifacts indiscriminately. For failures, inspect the
public PR and state, then retry from a fresh checkout; never reset a user's dirty
worktree automatically. Fix source freshness or repository protection settings
before retrying. GitHub's five-minute schedule may be delayed; it is not an SLA.
GitHub can disable schedules after 60 days without repository activity. Check
workflow status manually and deliberately re-enable only if desired; this project
does not make artificial keepalive commits.

## Rollback

First set `SENTINEL_ENABLED=false` and disable the GitHub workflow. Only then
close trigger PRs if desired. Disconnect the repository/environment from Codex
if retiring the integration. Repository deletion is a separate destructive
action requiring new approval. Pause the sentinel before buying credits or
enabling auto-reload later.
