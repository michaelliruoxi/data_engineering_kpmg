# AWS PostgreSQL deployment

The shared `sec` database is already deployed on Amazon RDS for PostgreSQL, with the local database and original SEC files retained. This guide records the completed setup and retains the deployment procedures for maintenance and recovery reference. Current team work uses the existing instance; outstanding teammate access is tracked separately from AWS setup.

## Current deployment status: October 6, 2026

The `sec-filings` RDS instance is Available in `us-east-1`: PostgreSQL 17.11, `db.t4g.micro`, 20 GiB encrypted gp3 storage, one-day automated backups, and deletion protection. Its security group allows TCP 5432 from the maintainer's current public IPv4 `/32`. A connection using the ignored `.env.aws` file succeeded with `verify-full` and TLS 1.3. The actual master username is `postgres`.

The import is complete. A fresh snapshot of local schema `sec` was restored transactionally into RDS. All nine tables (including the migration ledger) and three citation views matched by canonical full-row SHA-256 hashes; UUIDs and every stored field were included. View definitions and the relation inventory also matched. A fresh local comparison confirmed the source was unchanged, and the migration checksum check found no pending migrations.

Cloud counts are one company, one filing, four source documents, and zero processed reports, chunks, financial facts, or tables. The original source files remain local; this deployment copies database records, not file bytes.

The retained migration backup is `backups/sec_filings-rds-20261006T200820Z-030a1bf5.dump`. Its companion `.verification.json` records the backup hash, per-relation counts and hashes, TLS result, and final cloud status. Both are local and ignored by Git. Docker was recovered by preserving only its stale socket-only runtime directories and restarting; the PostgreSQL volume and Docker settings were retained.

The October 6 account setup recorded in "Clarify teammate AWS database access" created and verified six individual logins in the `sec_reader` group: `sec_jace`, `sec_bryce`, `sec_jazzy`, `sec_emma`, `sec_ruby`, and `sec_sally`. All six logins were tested from the already-allowed network and can read the SEC tables/views without shared-data write or schema-creation permissions. Credentials are shared privately.

Network access from the teammates' own machines remains pending: the latest recorded AWS inbound rule still permits only the maintainer's public IPv4 address. Add approved teammate `/32` rules and test their connections before marking onboarding complete. Reader accounts do not need to be recreated. Michael retains shared writes initially; restricted loader permissions can be considered after local implementation and review.

To check the live database from this checkout:

```powershell
.\.venv\Scripts\python.exe -m sec_pipeline.db_cli --env-file .env.aws status
```

## 1. Sign in and check billing

Open the [AWS console](https://console.aws.amazon.com/console/home). Complete sign-in or account creation directly in AWS. Keep passwords, payment information, and verification codes out of chat and tracked files.

In Billing and Cost Management, check the account plan, Free Tier status, credit balance, and expiration dates. Set a monthly cost budget with email alerts before creating the database; a budget alert does not cap spending. Free-plan credits and eligibility are account-specific. Sharing PostgreSQL credentials does not require inviting teams into AWS Organizations.

## 2. Review the initial configuration

Use RDS **Standard create** so the settings are visible. Select a Free tier template if the account offers it; otherwise use the development/sandbox option and review every value. Console labels may differ by account plan.

| Setting | Initial value |
| --- | --- |
| Region | US East (N. Virginia), `us-east-1` |
| Engine | PostgreSQL 17, current supported minor version |
| Deployment | Single-AZ DB instance |
| Identifier | `sec-filings` |
| Initial database name | `sec_filings` |
| Master username | `postgres` |
| Instance | `db.t4g.micro` |
| Storage | 20 GiB gp3, encrypted |
| Storage autoscaling | Off initially; monitor free space and increase deliberately |
| Port | 5432 |
| Connectivity | No additional EC2 instance or RDS Proxy |
| Public access | Yes for the proposed direct laptop connection; restrict the security group as below |
| Security group | A dedicated group, TCP 5432 from the maintainer's current public IPv4 `/32` only |
| Credentials | Self managed for this starter estimate; enter and retain the password directly in a password manager |
| Automated backups | 1 day for the Free plan; use longer retention only if the account plan allows it |
| Deletion protection | Enabled |
| Monitoring | Basic/standard options; review charges before enabling additional monitoring |

Public accessibility provides a network endpoint; the security group still controls who can reach it. Never add `0.0.0.0/0` or `::/0`. Add approved teammate source addresses separately. If the institution requires private access, choose a private-network design before creating the database; the connection steps and cost estimate will change.

For 730 running hours, current `us-east-1` on-demand pricing gives approximately $11.68 compute + $2.30 storage + $3.65 for one public IPv4 address = **$17.63/month before credits**. This is a baseline, not a cap. Taxes, CPU bursting, additional backup storage, traffic, and optional services can add charges. Review AWS's creation summary before submission. No NAT gateway, VPN, EC2 instance, proxy, or paid support is included in this estimate.

After creation, wait for **Available**, then record the endpoint hostname. Verify the final security-group rule and configuration before connecting.

## 3. Configure a verified TLS connection

Use a separate ignored file named `.env.aws`; leave the local `.env` unchanged. Replace the placeholders below with the selected RDS endpoint and credentials. Use literal values, with no inline comments or variable interpolation. Keep the file private.

```dotenv
PGHOST=REPLACE_WITH_RDS_ENDPOINT
PGPORT=5432
PGDATABASE=sec_filings
PGUSER=postgres
PGPASSWORD=CHANGE_ME
PGSSLMODE=verify-full
PGSSLROOTCERT=.cache/aws-rds/us-east-1-bundle.pem
```

Run commands from the repository root so the certificate path resolves correctly. Process environment values override file values; check for old `PG*` settings if the connection targets an unexpected database. `DATABASE_URL` is not consumed by the current connection helper.

The regional CA bundle can be downloaded from AWS without any account credentials:

```powershell
New-Item -ItemType Directory -Force .cache\aws-rds | Out-Null
Invoke-WebRequest -Uri 'https://truststore.pki.rds.amazonaws.com/us-east-1/us-east-1-bundle.pem' -OutFile .cache\aws-rds\us-east-1-bundle.pem
```

`verify-full` validates both the issuing CA and the endpoint hostname. Keep it enabled. A different region needs the matching regional CA bundle.

## 4. Copy the existing database

Start Docker Desktop and the existing local database. Use the [backup workflow](database.md#backup) to produce a fresh, uniquely named custom-format dump after confirming local status. Preserve all existing backups.

Scope the fresh archive to **schema `sec`** using `pg_dump --schema=sec -Fc`, then restore that entire scoped archive into the new, empty RDS database using PostgreSQL 17 `pg_restore` and verified TLS. Use `--no-owner --no-privileges --exit-on-error --single-transaction`. Omit `--schema` from the restore command so the archive's `CREATE SCHEMA` entry is included. The local administrator role and grants should not be copied into RDS. The existing Docker container supplies a compatible client if none is installed on Windows. Copy the dump and CA bundle into that container before restoring from it, and use the corresponding container paths.

Supply passwords using an interactive password prompt or a private credential mechanism, never a command argument or connection URL. Do not use `--clean`, overwrite another populated database, or seed before restoring. Dump/restore preserves existing UUIDs, provenance, processed records, and the migration ledger.

Once restored, verify with the existing command:

```powershell
.\.venv\Scripts\python.exe -m sec_pipeline.db_cli --env-file .env.aws migrate
.\.venv\Scripts\python.exe -m sec_pipeline.db_cli --env-file .env.aws status
```

For a current-schema dump, `migrate` should apply nothing and validate the existing migration hashes. Compare all table counts, record IDs, and citation views against the local source. The documented sample contains one company, one filing, four source documents, and no processed content; use fresh source counts if loaders have since added records. Check `pg_stat_ssl` for the active connection and test reconnecting.

The source files and `source_manifest.json` are not contained in the database. Retain matching local copies for each loader until shared object storage and its file-resolution support are implemented.

## 5. Share controlled access

The six current teammate logins already belong to the read-only `sec_reader` group. Reuse these accounts for onboarding. For future people or services, create separate logins and assign appropriate group roles. Reserve schema migrations, role administration, and database ownership for Michael. Keep shared imports with Michael until a reviewed loader needs a restricted write role; scope any grant to its actual tables and operations, without schema administration or overwriting source provenance.

Each team needs the endpoint, port, database name, its own login, the CA bundle, and instructions to use `verify-full`. Share credentials privately. Add only approved source IPs to the security group, and verify access from another team's machine before calling cross-team sharing complete. Local success alone does not prove another team's network can connect.

## References

- [RDS creation settings](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_CreateDBInstance.html)
- [RDS networking](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_VPC.WorkingWithRDSInstanceinaVPC.html)
- [RDS certificate bundles](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html)
- [Regional RDS price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/us-east-1/index.json)
- [Public IPv4 pricing](https://aws.amazon.com/vpc/pricing/)
- [Free Tier rules](https://aws.amazon.com/free/free-tier-faqs/)
