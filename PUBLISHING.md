# Releasing & publishing

Releases follow the Lakekeeper org convention: **release-please** drives versioning and
changelogs from [Conventional Commits](https://www.conventionalcommits.org/); merging the
release PR tags the release and publishes to PyPI via **Trusted Publishing** (OIDC, no tokens).

## How a release happens

1. Land conventional commits on `main` (`feat:`, `fix:`, `docs:`, …). For Python, scope them
   under `python/` (e.g. `feat(python): …`).
2. [`release-please`](.github/workflows/release.yml) keeps an open **"chore: release python"**
   PR with the next version + generated `python/CHANGELOG.md`.
3. **Merge that PR.** release-please then:
   - tags `python-vX.Y.Z` and creates the GitHub release,
   - bumps `version` in `python/pyproject.toml`,
   - triggers `publish-python`, which builds the sdist+wheel and publishes `pylakekeeper` to PyPI.

Versioning is per-component (`python-vX.Y.Z`); Java/Spark get their own components when added
(`release-please/release-please-config.json`).

## One-time setup (admin actions — not in code)

### 1. PyPI Trusted Publisher
Create a *pending publisher* so the first OIDC publish can create the project:

- PyPI → your account → **Publishing** → *Add a pending publisher*:
  - PyPI Project Name: `pylakekeeper`
  - Owner: `lakekeeper` · Repository: `lakekeeper-clients`
  - Workflow name: `release.yml`
  - Environment name: `pypi`
- Create a GitHub **Environment** named `pypi` in repo settings (the workflow references it).
- (Recommended) Repeat on **TestPyPI** and do a dry run first — see below.

Secrets/vars already in place:
- `PLEASE_RELEASE_LK_C` — token used by the release-please action.

### 2. CLA (contributor license agreement)
Lakekeeper repos use the hosted **CLA Assistant** app (the `license/cla` check via
`https://cla-assistant.io/lakekeeper/<repo>`), not a workflow file. Enable it for this repo:

- Go to <https://cla-assistant.io>, sign in as a `lakekeeper` org admin.
- Link `lakekeeper/lakekeeper-clients` to the **same CLA gist** used by `lakekeeper/lakekeeper`.
- (Recommended) Make `license/cla` a required status check in branch protection for `main`.

There is nothing to add in this repo for the CLA — it is configured in the app.

## Local dry run (before the first real publish)

```sh
cd python
python -m build                 # produces dist/pylakekeeper-*.whl + .tar.gz
python -m twine check dist/*    # metadata sanity
# optional: upload to TestPyPI, then: pip install -i https://test.pypi.org/simple/ pylakekeeper
```
