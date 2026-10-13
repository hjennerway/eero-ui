# 🔄 CI/CD Pipeline

The fork publishes its own container images through `.github/workflows/docker-publish.yml`.

```
Push / manual run → application tests → container startup check → GHCR
```

## Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| **CI Pipeline** | Push/PR | Runs commit lint, backend tests, frontend tests, security scan |
| **Release** | After CI passes | Creates GitHub releases using semantic-release |
| **Publish GHCR image** | Push to `master` or `codex/publish-ghcr`, `v*` tags, or manual run | Tests the application, smoke-tests the container, and publishes AMD64/ARM64 images |
| **Sync Wiki** | Push to `wiki/` | Auto-updates GitHub Wiki |

## Conventional Commits

Use [Conventional Commits](https://conventionalcommits.org) to trigger automatic releases:

| Commit Type | Example | Version Bump |
|-------------|---------|--------------|
| `feat:` | `feat: add device filtering` | Minor (1.x.0) |
| `fix:` | `fix: handle empty device list` | Patch (1.0.x) |
| `feat!:` | `feat!: redesign API` | Major (x.0.0) |

## Docker Image

Images are published to:

```
ghcr.io/hjennerway/eero-ui:latest
ghcr.io/hjennerway/eero-ui:master
ghcr.io/hjennerway/eero-ui:sha-<full-commit-sha>
ghcr.io/hjennerway/eero-ui:<version>
```

Multi-platform support: `linux/amd64`, `linux/arm64`

`latest` is updated only by builds of the default branch. Other branches get their own tag;
version tags produce a versioned image. Each successful run records the immutable
`ghcr.io/hjennerway/eero-ui@sha256:...` image reference in its summary.

Publishing uses the repository's automatically supplied `GITHUB_TOKEN` with
`packages: write`; no personal access token is needed in Actions. The package must
be public for unauthenticated pulls from Dockge, or the Docker host must already
be signed in to GHCR with package read access. See GitHub's
[container registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

To deploy in Dockge, replace the app service's `build:` section and local `image:`
with the published image reference, then deploy. Keep its existing ports,
environment and data volumes. The repository's `docker-compose.yml` remains the
local-build example. A digest pins an exact tested image; changing to a newer
digest and redeploying is an explicit update. Keep the previous image reference
for rollback without deleting any volumes. Publishing does not automatically
redeploy the LAN host.
