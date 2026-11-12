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

After a successful `master` push, the exact validated archive and its checksum
are uploaded to a draft Release, verified, and published. Immutable Releases
must be enabled in the repository or owner settings. The publisher verifies the
final immutable flag, tag commit, uploaded asset sizes, and server digests.
It can resume a matching incomplete draft, but never overwrites conflicting
assets or treats an ambiguous write as permission to repeat it immediately.

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

Change source pins through a PR. A source workflow change also requires an
explicit update to its job and workflow-blob policy. Existing successful native
results can be reused while the pins are historical; pins need not follow a
moving branch tip to remain valid.

The public distribution is intended to become the public base consumed by
`codex-private-workflows`. That consumer migration and source-event dispatch
credentials are separate integration steps. Creating a release here alone
does not change private pins or install anything into a real user home.

## Licences

The root Apache-2.0 `LICENSE` applies to this repository's aggregation code and
configuration. It does not relicense bundled source material. Original notices
are preserved: `grilling` includes its MIT licence and upstream attribution;
Archify includes MIT, third-party notices, and font licences. Other source
repositories do not currently declare a root licence; consult their owners
before redistribution beyond the permissions they grant. The provenance records
identify the exact source for every exported file.
