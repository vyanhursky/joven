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
`package` are green, so a release cannot go out on a red suite. Once PyPI has the
files, `release` creates the matching GitHub release: the same wheel and sdist
attached, the CHANGELOG section for that version as the notes, and `--prerelease`
set for any version carrying `a`, `b` or `rc`.

### The CHANGELOG guard

Step 2 is checked, not trusted. `package` runs
`tools/changelog_section.py <version>` on a tag and fails if there is no
`## v<version>` heading to extract — before the upload, because afterwards the
version number is spent whether or not the notes existed. The same script produces
the release notes, so what CI checks for is exactly what it will publish.

### The version guard

The `package` job refuses a tag that disagrees with `pyproject.toml`:

```
::error::tag v1.0.0b2 does not match pyproject version 1.0.0b1
```

This exists because a version number is the one thing a release cannot take back —
PyPI will not let you re-upload a version, even a deleted one. Catching the
mismatch before upload costs a few seconds; catching it afterwards costs a version
number.

## The downloadable app

The `app` job builds it on three runners — PyInstaller does not cross-compile —
and the `release` job attaches the three archives beside the wheel. Nothing here
needs doing by hand at release time; what follows is for changing how it is built.

```bash
python packaging/fetch_vendor.py     # kepubify, the one tool the app ships
pyinstaller packaging/joven.spec --noconfirm --distpath packaging/dist --workpath packaging/build
```

`packaging/vendor/` is gitignored and fetched fresh by every build, so bumping
`KEPUBIFY_VERSION` in `fetch_vendor.py` is the whole of a
dependency bump. Its licence permits redistribution with attribution, which
`packaging/NOTICE` provides and the build ships.

**onedir, not onefile**, and the reason is measured rather than assumed. Windows,
2026-09-17: 357 MB on disk, 220 MB zipped, ~260 ms to start; Ubuntu 24.04: 375 MB.
Since b8 stopped bundling epubcheck each is about 22 MB smaller: the macOS DMG
measured 223 MB before and 201 MB after.
and ~210 ms. onefile would unpack all of that to a temp directory on *every*
launch. The bulk is `lingua` — a single ~291 MB extension module with the language
models compiled in, which is also why the size cannot be reduced by supporting
fewer languages.

The builds are **unsigned**. Windows SmartScreen and macOS Gatekeeper both warn on
first run, and [docs/install-app.md](install-app.md) walks a reader through it.
Signing would cost about $99/yr for Apple plus a Windows certificate; it can be
added later without changing anything here except the CI job.

A build that omits `lingua` still starts and still serves the page, then dies at
the first paragraph of the first detect — so the `app` job runs the built binary
before packaging it. The check is on `doctor`'s *output*, not its exit code: no
runner has a model server, so it exits 1 by design.

## Checking it worked

```bash
uv tool install joven-ebook-annotator
joven --help
joven doctor
```

And, from the release page, download one app archive, extract it, and run it once.

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
