# Releasing

Cutting a release is one `git push` of a tag. Everything after that is CI.

The one thing CI cannot do for you is prove to PyPI that it is allowed to publish
under this project's name, so that part is set up once, by hand, below.

---

## One-time: register the trusted publisher

We publish with [Trusted Publishing][tp] rather than an API token. PyPI verifies
the workflow's OIDC identity directly, which means there is no long-lived secret
in the repository to leak, rotate, or forget about.

Because `joven-ebook-annotator` does not exist on PyPI yet, register it as a
*pending* publisher — PyPI creates the project the first time the workflow runs.

1. Sign in to <https://pypi.org> → **Your projects** → **Publishing** →
   **Add a new pending publisher**.
2. Fill it in exactly:

   | Field | Value |
   |---|---|
   | PyPI project name | `joven-ebook-annotator` |
   | Owner | `vyanhursky` |
   | Repository name | `joven` |
   | Workflow name | `ci.yml` |
   | Environment name | `pypi` |

3. In the GitHub repository, **Settings → Environments → New environment**, named
   `pypi`. Adding yourself as a required reviewer here is worth it: it turns every
   publish into something you approve, and a release is the one action that cannot
   be taken back.

The environment name must match on both sides. If they disagree, the publish step
fails at the point of upload with a permissions error, not before.

[tp]: https://docs.pypi.org/trusted-publishers/

## Every release

1. Bump `version` in `pyproject.toml`.
2. Move the `## Unreleased` section of [CHANGELOG.md](../CHANGELOG.md) under the
   new version number, dated.
3. Commit, tag, push:

   ```bash
   git tag v1.0.0b2
   git push origin main --tags
   ```

The tag triggers the full workflow. `publish` runs only after `test`, `guards` and
`package` are green, so a release cannot go out on a red suite.

### The version guard

The `package` job refuses a tag that disagrees with `pyproject.toml`:

```
::error::tag v1.0.0b2 does not match pyproject version 1.0.0b1
```

This exists because a version number is the one thing a release cannot take back —
PyPI will not let you re-upload a version, even a deleted one. Catching the
mismatch before upload costs a few seconds; catching it afterwards costs a version
number.

## Checking it worked

```bash
uv tool install joven-ebook-annotator
joven --help
```

A published version can be *yanked* (hidden from new installs) but never replaced.
If a release is wrong, yank it and ship the next patch version.

## Why there is no Homebrew formula

Worth writing down so it does not get proposed again without the constraint
attached.

`lingua-language-detector` publishes **no source distribution** — only
platform-specific wheels, each about 170 MB, with the language models compiled in.
Homebrew's Python tooling (`virtualenv_install_with_resources`,
`brew update-python-resources`) resolves resources from sdists, so a formula would
need hand-written wheel URLs and checksums per architecture *and* per CPython minor
version. Every `python@` bump in Homebrew, and every lingua release, would break it.

`uv tool install` gets the right wheel in one step. Two commands that keep working
beat one command that needs babysitting.
