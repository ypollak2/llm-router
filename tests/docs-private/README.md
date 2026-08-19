# Enterprise Documentation (Private)

This directory contains documentation for LLM Router's **enterprise-only features** that are not included in the open-source public distribution.

## Structure

- **identity/** — OIDC/SCIM provisioning, federated authentication
- **security/** — RBAC policies, audit chain, compliance
- **backends/** — Postgres multi-instance budget coordination
- **deployment/** — Enterprise Kubernetes, monitoring, operations

## Public vs Private

- `docs/` ← Public documentation (committed to GitHub, shipped with package)
- `docs-private/` ← Enterprise documentation (local-only, .gitignored)

This directory is **NOT committed to public GitHub** and is not shipped in the PyPI package.

## Internal Use Only

This documentation is for:
- Internal team reference
- Enterprise customer deployments
- Private documentation builds (if needed)

See `/docs/` for public operator guides and API documentation.
