# Operations Notes

## Home Assistant add-on deploy

Set the Home Assistant connection details in your shell before running the
commands below:

```bash
export HA_HOST=<home-assistant-ip-or-hostname>
export HA_SSH_PORT=2222
export HA_SSH_KEY=~/.ssh/homeassistant
export HA_SSH_USER=root
```

SSH:

```bash
ssh -p "$HA_SSH_PORT" -i "$HA_SSH_KEY" "$HA_SSH_USER@$HA_HOST"
```

The add-on is installed as:

```text
/addons/local/gemini-live-proxy
```

## Important Supervisor behavior

Do not keep backups or copies of this add-on anywhere under `/addons`.

Home Assistant Supervisor scans local add-on folders and can become confused if
another folder contains a `config.yaml` with the same `slug`. This caused
Supervisor to keep seeing the old `local_gemini_live_proxy` version and to fail
with misleading errors such as:

```text
Cannot build app 'local_gemini_live_proxy' because dockerfile is missing
Image local/aarch64-addon-gemini_live_proxy:<version> does not exist
```

Keep backups outside `/addons`, for example:

```text
/config/gemini-live-proxy-backups/
```

## Preferred release model

Do not rely on Raspberry Pi / Home Assistant OS to build the Python add-on.
Publish prebuilt GHCR images and let Supervisor pull them.

The add-on config should include:

```yaml
image: "ghcr.io/marcinnowak79/gemini-live-proxy-{arch}"
```

Release flow:

```bash
ruby -e 'require "yaml"; %w[.github/workflows/docker-build.yml repository.yaml addon/config.yaml].each { |p| YAML.load_file(p); puts "OK #{p}" }'
docker build --platform linux/arm64 --build-arg BUILD_VERSION=<version> --build-arg BUILD_ARCH=aarch64 -t ghcr.io/marcinnowak79/gemini-live-proxy-aarch64:<version> addon
git add .github/workflows/docker-build.yml addon/Dockerfile addon/config.yaml
git commit -m "Build HA app from prebuilt GHCR image"
git tag v<version>
git push origin main v<version>
```

After GitHub Actions publishes the image:

```bash
rsync -av --exclude __pycache__ -e "ssh -p $HA_SSH_PORT -i $HA_SSH_KEY -o BatchMode=yes" addon/ "$HA_SSH_USER@$HA_HOST:/addons/local/gemini-live-proxy/"
ssh -p "$HA_SSH_PORT" -i "$HA_SSH_KEY" "$HA_SSH_USER@$HA_HOST" 'ha store reload'
ssh -p "$HA_SSH_PORT" -i "$HA_SSH_KEY" "$HA_SSH_USER@$HA_HOST" 'ha apps update local_gemini_live_proxy'
ssh -p "$HA_SSH_PORT" -i "$HA_SSH_KEY" "$HA_SSH_USER@$HA_HOST" 'ha apps start local_gemini_live_proxy'
```

Verify:

```bash
nc -vz "$HA_HOST" 8765
nc -vz "$HA_HOST" 8766
ssh -p "$HA_SSH_PORT" -i "$HA_SSH_KEY" "$HA_SSH_USER@$HA_HOST" 'ha apps info local_gemini_live_proxy | grep -E "^(build|state|version|version_latest):"'
ssh -p "$HA_SSH_PORT" -i "$HA_SSH_KEY" "$HA_SSH_USER@$HA_HOST" 'ha apps logs local_gemini_live_proxy | tail -n 80'
```

Expected healthy state:

```text
build: false
state: started
version: <current>
version_latest: <current>
```

Ports:

```text
ws://<home-assistant-host>:8765
http://<home-assistant-host>:8766/response/<session>.wav
```
