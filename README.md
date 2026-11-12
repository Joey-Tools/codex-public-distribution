# Codex Public Distribution

A public installable bundle assembled from exact revisions of the Joey-Tools
source repositories. Source code stays in its canonical repository; this
repository owns source selection, composition, package integration, and release.
It contains no personal configuration or private workflow adaptations.

## Package contents

`distribution.json` selects four command helpers and twelve skills from six
public repositories. `sources.lock.json` records each commit, tree, repository
identity, and native CI workflow blob. `source-ci-policy.json` states which
native jobs must have succeeded for those exact commits.

The package contains public Toolbox helpers, review and delivery workflows,
debug triage, workflow hygiene, project journal, and Archify. Private tracker
integrations, remote host configuration, personal instructions, agents, and
private skill overrides remain in `codex-private-workflows`.

## Validation and release

The source repositories test their own implementations. Distribution CI reads
GitHub's native run and job APIs and requires the latest matching push run for
every locked SHA and default branch to pass. A matching failure, missing run,
unknown job, pending rerun, or unavailable API blocks composition. The audit
JSON records the API observation; it is not an alternative test credential.

Distribution CI tests aggregation and publication logic, reads committed Git
blobs, verifies source trees, and builds one deterministic archive. Linux and
macOS install it and upgrade from the existing Toolbox public package in
temporary home directories. Public-layer uninstall is intentionally unsupported
by the existing installer; the gate verifies that refusal preserves the install
and user files. The source test matrices are not repeated here.

After a successful `master` push, distribution inputs are compared with the latest
complete immutable Release's commit. If they differ, the exact validated archive
and its checksum are uploaded to a draft Release, verified, and published. A CI
repair can therefore publish source changes whose earlier publication failed.
Immutable Releases
must be enabled in the repository or owner settings. The publisher verifies the
final immutable flag, tag commit, uploaded asset sizes, and server digests.
It can resume a matching incomplete draft, but never overwrites conflicting
assets or treats an ambiguous write as permission to repeat it immediately.
Automation and documentation changes still run CI, but do not publish an identical
public payload under another SHA and trigger an unnecessary private-base update.
Source locks, export configuration, builders, native-source policy and publisher
changes remain publication inputs.

Release names follow `personal-codex-YYYYMMDD-HHMMSS-<short-sha>`. Each release
contains `personal-codex-<full-sha>.tar.gz` and `personal-codex-<full-sha>.sha256`. The archive
uses the existing public `codex-personal-sync` installation format. It includes
the source lock and per-file provenance as reference files.

On a public-only machine with the public sync runner already bootstrapped, preview
an upgrade with:

```sh
"$HOME/.codex/bin/codex-personal-sync" install \
  --repo Joey-Tools/codex-public-distribution \
  --home "$HOME/.codex" --dry-run
```

Apply it by removing `--dry-run`. Private overlay installations should wait for
the consumer migration described below so ownership transfers remain explicit.

## Local build

Check out each locked repository under `.sources/<source-name>` with its Git
objects available. Python 3.13 and Node.js 22 are used by the integration gate.

```sh
python3 -B -m unittest discover -s tests -v
python3 -B scripts/source_ci.py
python3 -B scripts/distribution.py --sources-root .sources --output-dir dist --sha <distribution-commit>
python3 -B scripts/package_smoke.py --dist dist --sha <distribution-commit>
```

The build reads pinned objects rather than mutable checkout files. It rejects
symlinks, submodules, unsafe or colliding export paths, missing skills, and
unsupported transforms. Archify's standalone package metadata is the only
explicit source adaptation; its runtime files retain their original bytes.

## Updating sources and private composition

`Update public sources` checks the six configured source refs every eight hours
and supports manual dispatch. Only commit/tree pins can change automatically;
the updater verifies every candidate against the existing native-CI policy before
writing a lock. A source workflow change requires an explicit policy update.
Unchanged refs create no commit or PR. Historical successful native results remain
usable for their exact pins; they do not have to follow a moving branch tip.

The public writer creates a source-only PR with a GitHub-signed commit. The normal
Linux/macOS distribution CI validates it, and a completion event wakes the writer
to merge only the exact successful head. Pending or failed CI, unknown jobs,
unresolved threads, changed scope or unrelated PRs cannot select a merge. App
commits and merges trigger the native PR/push workflows; no event is disguised as
a push and no generated success receipt replaces native CI.

After a newly published immutable release, an enabled notifier wakes
`codex-private-workflows`' existing `scheduled-sync-release.yml` with `force=true`.
It asks private to resolve the latest release, so a delayed notification cannot
select an older event tag. The private repository retains its own PR review and
approval rules. Reusing an already published release sends no second notification.
Creation of a release here does not install anything into a real user home.

### Automation configuration

Configure two GitHub Apps and install each only on its target repository:

| Role | Repository | Permissions |
| --- | --- | --- |
| Public source writer | `Joey-Tools/codex-public-distribution` | Contents and Pull requests: write; Actions, Checks and Commit statuses: read |
| Private wakeup | `Joey-Tools/codex-private-workflows` | Actions: write |

Do not add either App to a ruleset bypass list. These are independent App grants:
one App installed on both repositories would grant its combined permissions to
both. Narrow installation tokens do not narrow what the underlying private key
can mint. [GitHub documents token scoping here](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app).

In this public repository's Actions configuration, set:

| Variable | Secret containing its App private key |
| --- | --- |
| `DISTRIBUTION_BOT_APP_CLIENT_ID` | `DISTRIBUTION_BOT_APP_PRIVATE_KEY` |
| `DISTRIBUTION_NOTIFY_APP_CLIENT_ID` | `DISTRIBUTION_NOTIFY_APP_PRIVATE_KEY` |

Use each App's **Client ID** from its settings. Put the PEM keys directly into
GitHub Secrets. The workflow mints short-lived tokens for the named repository
and permissions using GitHub's official App-token action.

First configure the public writer and manually run `Update public sources` on
`master`; confirm its source-only PR or explicit no-change result. Without the
writer variable, the workflow reports that updates are unconfigured and performs
no source inspection or mutation. Then deploy the private consumer support and
verify its default-branch Release. Only afterward set the public Actions variable
`PRIVATE_OVERLAY_DISTRIBUTION_READY=true`. Test the wakeup by manually dispatching
`Update public sources` with `notify_private=true` and observe the matching private
workflow; that mode does not refresh sources or publish a release.

Keep private's scheduled and manual recovery until a natural publication proves
delivery and missed-event recovery. Public scheduled workflows can be disabled
after [60 days without repository activity](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows).
Re-enable `update-sources.yml` and dispatch it to catch up; no keepalive commits
are created. Automatic recovery from public inactivity remains a separate follow-up.

## Licences

The root Apache-2.0 `LICENSE` applies to this repository's aggregation code and
configuration. It does not relicense bundled source material. Original notices
are preserved: `grilling` includes its MIT licence and upstream attribution;
Archify includes MIT, third-party notices, and font licences. Other source
repositories do not currently declare a root licence; consult their owners
before redistribution beyond the permissions they grant. The provenance records
identify the exact source for every exported file.
