# changelog.d

One file per PR: `<id>.<type>.md`, where `<type>` is one of `added`, `changed`,
`deprecated`, `removed`, `fixed`, `security`, `docs`, `internal`. Example:
`N21.fixed.md`, `HAIKU55-TIER-1.added.md`. The body is the bullet text (no leading
`- ` needed). Do not edit `CHANGELOG.md` in a PR. `scripts/changelog_fragments.py
assemble` folds fragments into `## [Unreleased]` at release and deletes them.
