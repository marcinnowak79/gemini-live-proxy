# Release Checklist

Use this checklist before publishing a new version.

## Before Release

1. Update `addon/config.yaml`:
   - bump `version`
   - update `image` if publishing prebuilt images
2. Update `addon/translations/en.yaml` when adding, removing, or renaming add-on options.
3. Update `addon/CHANGELOG.md`.
4. Run validation:

```bash
python3 -m py_compile proxy_server.py gemini_session.py ha_client.py addon/proxy_server.py addon/gemini_session.py addon/ha_client.py
python3 - <<'PY'
import yaml
for path in ["repository.yaml", "addon/config.yaml"]:
    with open(path) as f:
        yaml.safe_load(f)
    print(f"OK {path}")
PY
```

5. Run a secret scan:

```bash
rg -n "AIza|api[_-]?key\\s*[:=]|password\\s*[:=]|Bearer\\s+|192\\.168\\.|/Users/|\\.local" . \
  --glob '!**/.git/**' \
  --glob '!**/.env' \
  --glob '!**/venv/**' \
  --glob '!**/__pycache__/**'
```

Review every hit manually. Placeholders in examples are acceptable; real keys, tokens, local IPs and private paths are not.

6. Build the add-on locally or with GitHub Actions.
7. Test install/update on Home Assistant.
8. Tag the release:

```bash
git tag v1.0.2
git push origin v1.0.2
```

9. Create a GitHub Release using the `CHANGELOG.md` entry.

## Recommended Public Release Model

For early testing, Home Assistant can build the add-on locally from source.

For a better public user experience, publish a prebuilt image to GHCR and set `image` in `addon/config.yaml`, for example:

```yaml
image: "ghcr.io/YOUR_GITHUB_USERNAME/gemini-live-proxy"
```

Prebuilt images install faster and avoid build failures on users' Home Assistant hosts.
