# gotreat-de/.github

The org-wide repository of GoTreat. It holds:

- the one reusable deploy workflow, [`deploy.yml`](.github/workflows/deploy.yml), and its script
  [`deploy-dokploy.sh`](.github/scripts/deploy-dokploy.sh) - so every GoTreat repo deploys in exactly the same way,
  and a fix lands in all of them through one version bump;
- the org profile shown on [github.com/gotreat-de](https://github.com/gotreat-de), in [`profile/`](profile/).

`deploy.yml` stores the released `compose.yml` in a Dokploy compose service, deploys it through the Dokploy API,
waits for the deployment to end and verifies the running version. It is the only code that talks to Dokploy. The
decision record is
[`release-flow.md`](https://github.com/gotreat-de/website/blob/main/docs/specs/release-flow.md). This repo is
public because a public repo cannot call workflows from a private one.

What stays in each calling repo: `ci.yml` (job `check`), `release.yml` (release-please) - they differ per stack -
plus the thin `deploy.yml` below.

## Calling `deploy.yml`

Each repo keeps a thin `deploy.yml`: the manual entry point (`workflow_dispatch`) and what its `release.yml`
calls after a release.

```yaml
name: Deploy
run-name: Deploy v${{ inputs.version }} to production

on:
  workflow_call:
    inputs:
      version:
        required: true
        type: string
      sha:
        required: false
        default: ""
        type: string
  workflow_dispatch:
    inputs:
      version:
        description: "Released version that must be running afterwards, without leading v"
        required: true
        type: string
      dry_run:
        description: "Only verify Dokploy API access, deploy nothing"
        required: true
        default: false
        type: boolean

permissions:
  contents: read

jobs:
  deploy:
    name: Deploy
    uses: gotreat-de/.github/.github/workflows/deploy.yml@0000000000000000000000000000000000000000 # vX.Y.Z
    with:
      version: ${{ inputs.version }}
      sha: ${{ inputs.sha || github.sha }}
      dry_run: ${{ inputs.dry_run || false }}
    secrets: inherit
```

Replace the placeholder SHA with the commit of the release tag you want (see [Versions](#versions)).

Do not give the caller a `concurrency` group named `deploy-production`: the shared job holds that group, and a
caller that declared it too would wait for itself.

Configuration, all of it in the **calling** repo:

| Where | Name | Value |
|---|---|---|
| Repo root | `compose.yml` | the service's compose; at least one `image:` carries the release tag `vX.Y.Z` (release-please's annotated `image:` lines). Every deploy stores it in Dokploy as raw source, so an edit in Dokploy's UI lasts only until the next deploy |
| Variable of the repository or its org | `DOKPLOY_BASE_URL` | the Dokploy instance, HTTPS |
| Environment `production`, secret | `DOKPLOY_API_KEY` | API key of the deploying Dokploy user |
| Environment `production`, variable | `DOKPLOY_COMPOSE_ID` | id of the compose service |
| Environment `production`, variable | `HEALTH_URL` | `GET` endpoint answering `{"status": "ok", "version": "X.Y.Z"}`; leave it unset for a service without HTTP - the deployment status is then the whole verification |

The environment's deployment branches are restricted to `main`, and the job skips every other ref. The job runs
the deploy script of the ref it was called at (`job.workflow_repository` / `job.workflow_sha`), never code of
the calling repo; the caller's `compose.yml` of the released commit comes through the GitHub API as data, read
with the caller's token (`contents: read`). It does not roll back: a failed deployment or a wrong version is a
red run.

## Versions

Releases are annotated git tags `vX.Y.Z` on `main`. There is no moving major tag: callers pin the workflow by
the full commit SHA of a release, with the version as a comment, and Dependabot's `github-actions` group in the
caller bumps it as soon as a new tag exists.

```
just release 1.2.3        # on an up-to-date main: tags v1.2.3 and pushes it
```

- Try a change before releasing it: point one caller at the branch (`...@my-branch`) in a PR of that repo.
  `deploy.yml` only runs for `main`, so its proof is a `dry_run` dispatch after the release.
- A change callers have to follow (a renamed input, a new required variable) goes into a release whose notes say
  so; the Dependabot PR of each caller carries the adaptation.
- Roll back a caller by pinning the earlier SHA (and version comment) in its `deploy.yml`; nothing changes for
  the other callers.

## Development

`just check` - actionlint (with shellcheck on every `run:` block), shellcheck on the script, ruff, and the
tests: the deploy script against an in-process fake of the Dokploy API, and the rules the workflow must keep.
CI runs the same as the job `check`.

Needs `just`, `uv`, `actionlint`, `shellcheck`, `curl` and `jq`.
