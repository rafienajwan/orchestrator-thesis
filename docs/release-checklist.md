# Release Checklist

## Pre-release quality gates

Run from project root:

```powershell
.\.venv\Scripts\Activate.ps1
ruff check .
black --check .
mypy .
pytest -q
```

Expected result: all commands pass.

## Clean workspace for final archive

Artifacts that must not be included:
- .venv/
- venv/
- .pytest_cache/
- .mypy_cache/
- .ruff_cache/
- __pycache__/
- *.pyc
- .coverage

## Create clean zip

```powershell
.\scripts\create_clean_zip.ps1
```

Custom output name:

```powershell
.\scripts\create_clean_zip.ps1 -OutputPath .\orchestrator-thesis-final.zip
```

Include .env (only if explicitly needed):

```powershell
.\scripts\create_clean_zip.ps1 -IncludeEnvFile
```

## Final verification

1. Extract the generated zip into a temporary folder.
2. Confirm excluded folders are not present.
3. Run quick checks again in extracted folder:

```powershell
ruff check .
pytest -q
```
