# LLMCacheX — Release Checklist (Phase 15A: first public release prep)

Distribution: `llmcachex-core` · Import package: `llmcachex` · Version: `0.1.0`

## PRE-PUBLISH (verified during Phase 15A unless noted)

- [x] Package distribution name is `llmcachex-core`
- [x] Import name remains `llmcachex` (`src/llmcachex/`, `py.typed` ships)
- [x] Version correct (`0.1.0`, single-sourced from `llmcachex.__version__`)
- [x] Tests pass (`python -m pytest` — 313 passed, warning-free)
- [x] Redis tests pass (`python -m pytest -m redis` — needs local Redis)
- [x] Semantic tests pass (`python -m pytest -m semantic`)
- [x] Lint clean (`ruff check backend`), frontend builds (`npm run build` + `npm run lint`)
- [x] No real secrets found (repo-wide scan: placeholders/test values only)
- [x] `.env` ignored (root `.gitignore`: `.env`, `.env.*`); `.env.example` placeholders only
- [x] Frontend contains no backend secrets (only `VITE_API_BASE_URL`)
- [x] README correct (`pip install llmcachex-core`, `from llmcachex import cached_call`, extras documented)
- [x] CHANGELOG correct (capabilities through Phase 13, no invented releases)
- [x] License present (`backend/LICENSE`, MIT, matches `pyproject.toml` + author)
- [x] Wheel builds (`dist/llmcachex_core-0.1.0-py3-none-any.whl`)
- [x] sdist builds (`dist/llmcachex_core-0.1.0.tar.gz`)
- [x] twine check passes (`twine check dist/*`)
- [x] Clean wheel install works (fresh venv: import + SQLite cached call, no extras needed)
- [x] Optional extras verified (`api`, `dev`, `gemini`, `redis`, `semantic` in metadata)
- [x] API startup verified (`/api/health` returns status ok + `cache_backend`)
- [x] CI configuration verified (`.github/workflows/ci.yml` — test + build jobs, **no publish step**)
- [x] PyPI publication NOT performed

## MANUAL GITHUB (user performs after verification)

- [ ] Create GitHub repository
- [ ] Initialize Git (`git init`; do NOT commit secrets — see `.gitignore`)
- [ ] Review `git status` (only intended source files)
- [ ] Commit
- [ ] Configure remote
- [ ] Push

## MANUAL PYPI (user performs after verification)

- [ ] Create/verify PyPI account
- [ ] Enable 2FA if required
- [ ] Create API token (store in a secret manager — never in the repository)
- [ ] Upload distributions (`twine upload dist/*`)
- [ ] Verify project page (`llmcachex-core`)
- [ ] Install from PyPI in a fresh environment (`pip install llmcachex-core`)
- [ ] Test import (`from llmcachex import cached_call`)
