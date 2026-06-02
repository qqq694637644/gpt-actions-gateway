# Windows CI/CD deployment

This repository includes `.github/workflows/deploy-windows.yml` to deploy automatically when `main` receives a new push or merged PR. It can also be run manually from the GitHub Actions UI with `workflow_dispatch`.

## How it works

1. GitHub Actions checks out the repository after a `main` update.
2. The workflow creates a source zip package without `.git` or `.github`.
3. The package is copied to the Windows deployment server through OpenSSH.
4. A remote PowerShell script expands the package into a new release directory.
5. Existing `.env`, `data`, and common virtual environment directories from the current deployment are preserved.
6. The `current` directory is swapped to the new release.
7. If a Windows service name is configured, that service is stopped before the swap and started after the swap.

The deployment layout on the server is:

```text
<WINDOWS_DEPLOY_PATH>/
  current/                 # active source tree
  releases/<commit-sha>/   # extracted release packages
  previous-YYYYMMDDHHMMSS/ # last active source tree after each deployment
```

## Required GitHub repository secrets

Configure these in GitHub: **Settings -> Secrets and variables -> Actions -> Repository secrets**.

| Secret | Required | Example | Description |
| --- | --- | --- | --- |
| `WINDOWS_DEPLOY_HOST` | Yes | `203.0.113.10` | Windows deployment server host or IP. |
| `WINDOWS_DEPLOY_USER` | Yes | `deploy` | Windows account used for OpenSSH login. |
| `WINDOWS_DEPLOY_SSH_KEY` | Yes | private key content | Private key that can log in as `WINDOWS_DEPLOY_USER`. |
| `WINDOWS_DEPLOY_PATH` | Yes | `C:\services\gpt-actions-gateway` | Deployment root on the Windows server. |
| `WINDOWS_DEPLOY_PORT` | No | `22` | OpenSSH port. Defaults to `22`. |
| `WINDOWS_DEPLOY_SERVICE` | No | `gpt-actions-gateway` | Windows service to restart after deployment. |

## Windows server prerequisites

- Install and enable Windows OpenSSH Server.
- Add the public key matching `WINDOWS_DEPLOY_SSH_KEY` to the deploy user's `authorized_keys`.
- Give the deploy user read/write permission to `WINDOWS_DEPLOY_PATH`.
- If `WINDOWS_DEPLOY_SERVICE` is set, allow the deploy user to stop and start that service.
- Keep production-only files such as `.env`, runtime data, and any local virtual environment under `current`; the workflow preserves `.env`, `data`, `.venv`, `venv`, and `myvenv` across releases.

## First deployment

Before the first automatic deployment, create `WINDOWS_DEPLOY_PATH` or ensure the deploy user can create it. If the app already exists from manual zip deployment, place it under:

```text
<WINDOWS_DEPLOY_PATH>\current
```

The next deployment will move that directory to `previous-YYYYMMDDHHMMSS`, create a fresh `current`, and copy over `.env` and `data` from the old `current`.

## Rollback

On the Windows server, stop the service, move the current directory aside, move the desired `previous-*` or `releases/<commit-sha>` directory back to `current`, then start the service again.
