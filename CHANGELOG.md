# Changelog - Malmars Local Creative Studio

## 0.1.0-alpha1 - 2026-11-23

The first alpha with one program in three separately purchasable versions.

### Added
- One app in three versions. The Full Studio Suite is the master build;
  Print Studio and 3D & Character Studio are cut from it. Same program,
  same code, smaller rail.
- A version list (`malmars-products.json`) that is the single source of
  truth for the versions, the apps on each rail, the prices and the art.
- A packaged build pins itself by its own executable file name. The
  `exe` field in the version list names the file; `exeAliases` keeps a
  build from before the version stamp on its own rail.
- A load screen per version, showing that version's own scene.
- A real customer install guide, licence, third-party notices, alpha build
  notes and SHA-256 checksums in every bundle.
- The storefront page for one app in three versions, with the free Print
  Station beta as the one download that is ready.

### Changed
- The version chooser is gone. There is no storefront, no version list and
  no `?hub=1` mode: each build opens its own rail.
- The publisher's own copy of the version list beside the executable wins
  over the copy inside it, so a version can be changed without a rebuild.

### Still open
- The application payloads do not travel with the Studio executable yet.
  The three builds are alpha tester builds, not customer downloads.
  See `design/fundraising_drafts/PUBLISH_READINESS_2026-11-23.md`.
