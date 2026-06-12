# deploy/secrets

Put production secret files here only on the deployment host. Do not commit real secret values.

- `pg_pw`: PostgreSQL password file for production-style Compose secrets.
- `qa_fernet_keys`: Fernet key ring for credential encryption.
- `qa_webapp_secret`: Web cookie signing secret.

The local `deploy/compose.yaml` keeps `deploy/env/local.env` as the default one-command path. Secrets are reserved for the production hardening step so local onboarding stays simple.
