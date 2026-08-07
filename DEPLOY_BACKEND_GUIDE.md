# Deploy — CI/CD por GitHub Actions

> **Regla de oro de este fork:** los deploys NUNCA crean ni destruyen recursos de
> Azure. Solo construyen imágenes y actualizan apps existentes. `azd provision`
> / `azd deploy` NO se usan (método del accelerator upstream; en este fork causó
> costos por recursos auto-creados y quedó proscrito).

## El flujo (desde 2026-08)

```
commit → push a stable/v4-baseline
  → PR a main
      → required checks (protección de rama):
          test                      (suite backend, uv.lock exacto)
          Backend (ruff + mypy)     (quality-gate.yml)
          MCP server (ruff)         (quality-gate.yml)
          Frontend (ESLint + build) (quality-gate.yml)
  → squash-merge a main
      → cd.yml construye SOLO los componentes cuyo código cambió,
        taggea la imagen con el SHA exacto del commit y actualiza la app
  → ritual post-merge: realinear stable/v4-baseline al squash de main
      git fetch origin && git reset --hard origin/main \
        && git push --force-with-lease origin stable/v4-baseline
```

Lo que corre en producción siempre es un commit de `main`, identificable por su
tag `sha-<commit>` — nada de numeración manual (`fix26`, `fix27`, …).

## Destinos (recursos EXISTENTES, RG `arg-macaev4-8d1cceac`)

| Componente | Imagen (ACR `boatrentalacr`) | Destino | Update |
|---|---|---|---|
| Backend | `macaebackend:sha-<commit>` | Container App `ca-pslc25991vme66zmins` | `az containerapp update` |
| MCP server | `macaemcp:sha-<commit>` | Container App `ca-mcp-pslc25991vme66zmins` | `az containerapp update` |
| Frontend | `macaefrontend:sha-<commit>` | Web App for Containers `app-pslc25991vme66zmins` | `az webapp config container set` |

## Identidad

`cd.yml` se autentica por **OIDC federado** — sin secretos de contraseña:

- Service Principal: `copiloto-cli-sp` (roles: AcrPush en el ACR, Contributor).
- Federated credential: `repo:macae-labs/…:ref:refs/heads/main` (más los de
  environments prod/dev/integration).
- Secrets del repo: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`.

## Deploy manual (palanca de emergencia / catch-up)

Actions → **CD** → *Run workflow* → marcar los componentes a re-deployar
(checkboxes backend / mcp / frontend). Usa el HEAD de `main` y el mismo camino
que el deploy automático. Sirve para: re-deployar algo que entró a `main` antes
de que existiera el CD, o forzar un rollout sin cambio de código.

## Notas operativas

- **Dependabot**: PRs mensuales agrupados (ecosistema `uv` para backend/mcp,
  `npm` para frontend, `github-actions`), etiquetados `auto-merge`; el workflow
  de auto-merge solo arma el merge si `main` tiene required checks (falla
  cerrado). Ojo: un merge hecho por `GITHUB_TOKEN` **no dispara** cd.yml — esas
  actualizaciones llegan a producción con el siguiente merge humano o con la
  palanca manual.
- **Rollback**: re-deployar el SHA anterior con la palanca manual (o
  `az containerapp update --image …:sha-<commit-anterior>` como último recurso).
- El método consola (docker build/push + az update a mano) queda SOLO como
  último recurso si Actions está caído; si se usa, taggear con
  `sha-$(git rev-parse --short HEAD)` — jamás con numeración inventada.
