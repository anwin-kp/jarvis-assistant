# Contributing

## Branches

| Branch | Purpose |
|---|---|
| `main` | Stable, released code. Every release is tagged (`v0.1.0`, `v0.2.0`, ...). Only updated by merging `develop`. |
| `develop` | Integration branch. Finished features land here first. |
| `feature/<short-name>` | New features, branched from `develop`, e.g. `feature/phase-2-voice`, `feature/gaming-mode`. |
| `fix/<short-name>` | Bug fixes, branched from `develop`, e.g. `fix/weather-city-match`. |
| `hotfix/<short-name>` | Urgent fixes to a release, branched from `main` and merged into both `main` and `develop`. |
| `docs/<short-name>` | Documentation-only changes. |

Flow: `feature/*` → pull request → `develop` → (release) pull request → `main` → tag `vX.Y.Z`.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <short summary in imperative mood>
```

Types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `chore`, `ci`, `build`.

Examples:

```
feat(voice): add push-to-talk recording with webrtcvad
fix(weather): prefer the most populous city when names clash
docs: document the kill switch
```

## Versioning

[Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`. Each roadmap phase that lands on `main`
is a minor release (Phase 2 → `v0.2.0`).

## Before opening a pull request

```powershell
.\jarvis.ps1 test     # unit tests + ruff
```

CI runs the same checks on Windows for every push and pull request to `main` and `develop`.
