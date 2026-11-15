# How to release gimp-mcp-plugin

The server and the plugin ship together on PyPI as
[`gimp-mcp-plugin`](https://pypi.org/project/gimp-mcp-plugin/). Pushing a `vX.Y.Z` tag runs
`.github/workflows/publish.yml`, which builds the package and publishes it through Trusted
Publishing, so the repo stores no API token.

## 1. Choose the version

Versions follow [Semantic Versioning](https://semver.org/):
- **Patch** (`0.3.0` → `0.3.1`): bug fixes only.
- **Minor** (`0.3.0` → `0.4.0`): new tools, options or settings.

Each entry under `## [Unreleased]` in `CHANGELOG.md` is a candidate for the release.

## 2. Open a release PR

On a branch off an up-to-date `main` (for example `release-X.Y.Z`):

1. Bump the version in **three places**. They must match exactly: `check_server` and
   `get_gimp_info` warn when the plugin and server versions differ, and
   `tests/test_version_handshake.py` checks the three match:
   - `pyproject.toml` → `version`
   - `gimp_mcp_server.py` → `SERVER_VERSION`
   - `gimp-mcp-plugin.py` → `PLUGIN_VERSION`
2. Run `uv lock` so `uv.lock` records the new version.
3. Update `CHANGELOG.md`:
   - Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`.
   - Add a new, empty `## [Unreleased]` section above it.
   - Update the compare links at the bottom:
     ```
     [Unreleased]: https://github.com/gimp-mcp/gimp-mcp/compare/vX.Y.Z...HEAD
     [X.Y.Z]: https://github.com/gimp-mcp/gimp-mcp/compare/vPREV...vX.Y.Z
     ```
4. Check that the versions match:
   ```bash
   uv run python tests/test_version_handshake.py
   ```
   The tests in `tests/` are plain scripts, so run each one directly. `pytest tests` fails.
5. Open the PR titled `Release X.Y.Z`. [#111](https://github.com/gimp-mcp/gimp-mcp/pull/111)
   is a good example.
6. Merge it after the `ruff` check and CodeRabbit finish. `main` requires the branch to be up to
   date. Right after `gh pr update-branch`, `gh pr checks` may report "no checks" and the merge
   is refused, so wait for `ruff` to report before you merge.

A docs change that depends on CLI features that aren't released yet should merge right before
the tag, not earlier.

## 3. Tag and push

Tag the release PR's merge commit, not the tip of `main`. If another PR merged after the
release PR, tagging the tip would publish that code too, while its changelog entries still
sit under `[Unreleased]`.

```bash
git checkout main && git pull --ff-only
commit=$(gh pr view <release PR number> --repo gimp-mcp/gimp-mcp --json mergeCommit --jq .mergeCommit.oid)
git log --oneline -1 "$commit"   # should be "Release X.Y.Z (#...)"
git tag -a vX.Y.Z -m "gimp-mcp-plugin X.Y.Z" "$commit"
git push origin vX.Y.Z
```

The workflow refuses to build if the tag doesn't match the `pyproject.toml` version.

## 4. Approve the deployment

The `publish` job waits on the GitHub `pypi` environment, which needs a required reviewer's
approval:

Actions → the **Publish to PyPI** run for the tag → **Review deployments** → tick `pypi` →
**Approve and deploy**.

## 5. Verify

```bash
uvx --refresh gimp-mcp-plugin@X.Y.Z install-plugin --dry-run
```

PyPI takes a while to show a new upload:
- `https://pypi.org/pypi/gimp-mcp-plugin/json` can lag by several minutes. Check
  `https://pypi.org/pypi/gimp-mcp-plugin/X.Y.Z/json` instead.
- `uvx` can report "no version" for a minute or two after that. Wait until
  `https://pypi.org/simple/gimp-mcp-plugin/` lists the new wheel.

Then update your own setup:

```bash
uvx gimp-mcp-plugin@X.Y.Z install-plugin
```

Restart your MCP client so it starts the new server, and restart GIMP so it loads the new
plugin. If only one of them restarts, `check_server` reports a version mismatch.

## If something goes wrong

PyPI never accepts the same version number twice, even after you delete a release. To fix a bad
release, bump to the next patch version and release again.

## Reference

- **Trusted Publisher** (PyPI project settings): repo `gimp-mcp/gimp-mcp`, workflow
  `publish.yml`, environment `pypi`. PyPI rejects uploads from runs outside that environment,
  so a release can't skip the approval in step 4. If the environment is ever renamed, update
  it here too, or publishing fails.
- **GitHub environment** `pypi` (Settings → Environments): the required reviewer approves each
  release.
- **`astral-sh/setup-uv`** is pinned to an exact release (`@v10.3.3`). It hasn't published major
  tags since v8, so any bump needs an exact version.
- The PyPI names `gimp-mcp`, `gimp-mcp-server` and `gmcp` belong to unrelated projects.
