# AWS PostgreSQL deployment

The shared `sec` schema is already deployed in the `sec_filings` database on Amazon RDS for PostgreSQL, with the local database and original SEC files retained. This guide records the completed setup and retains the deployment procedures for maintenance and recovery reference. Current team work uses the existing instance; outstanding teammate access is tracked separately from AWS setup.

**For teammates:** use the [database access guide](../readme.md) and [manual example queries](manual-example.md). The provisioning, restore, and account-administration steps below are maintainer procedures; joining the project does not require creating or restoring a database.

## Current deployment status: October 7, 2026

The October 6 deployment record showed **Available** for the `sec-filings` RDS instance in `us-east-1`: PostgreSQL 17.11, `db.t4g.micro`, 20 GiB encrypted gp3 storage, one-day automated backups, and deletion protection. At that initial verification, its security group allowed TCP 5432 from the maintainer's public IPv4 `/32`. A connection using the ignored `.env.aws` file succeeded with `verify-full` and TLS 1.3. The actual master username is `postgres`. These instance and connection results are from October 6; check the live configuration before maintenance.

On October 7, AWS confirmed the saved inbound rule on `sec-filings-access` (`sg-013b476361af0d709`): **PostgreSQL TCP 5432 from `0.0.0.0/0` (Anywhere-IPv4)**. Individual IPv4 approval is no longer required. The earlier `/32` rules remain, but the broader rule makes IPv4 access unrestricted at this security group. No IPv6 Anywhere rule (`::/0`) was added. Any IPv4 host can attempt a database login; valid database credentials and permissions are still required. Keep clients on `verify-full` TLS with the AWS CA certificate.

The initial import is complete. A fresh snapshot of local schema `sec` was restored transactionally into RDS. At that point, all nine tables (including the migration ledger) and three citation views matched by canonical full-row SHA-256 hashes; UUIDs and every stored field were included. View definitions and the relation inventory also matched. A fresh local comparison confirmed the source was unchanged, and the migration checksum check found no pending migrations. The manual sample added afterward means current cloud and local content are no longer expected to match.

The initial cloud import contained one company, one filing, four source documents, and no processed content. On October 6, 2026, a separate, manually prepared `manual-example-v1` sample added **24 financial facts, 3 statement excerpts, 24 table-to-fact links, 1 report excerpt with 2 sections, and 6 chunks** to RDS. The sample was committed in one transaction and verified through a fresh TLS connection; existing metadata was unchanged. The six [sample queries](manual-example.md) passed under all six reader roles using the administrator's existing connection. Those role checks do not establish connectivity from teammates' own networks. This is partial example data, not completed pipeline output. The original source files and separate local database remain unchanged.

The retained migration backup is `backups/sec_filings-rds-20261006T200820Z-030a1bf5.dump`. Its companion `.verification.json` records the backup hash, per-relation counts and hashes, TLS result, and final cloud status. Both are local and ignored by Git. Docker was recovered by preserving only its stale socket-only runtime directories and restarting; the PostgreSQL volume and Docker settings were retained.

The October 6 account setup recorded in "Clarify teammate AWS database access" created and verified six individual logins in the `sec_reader` group: `sec_jace`, `sec_bryce`, `sec_jazzy`, `sec_emma`, `sec_ruby`, and `sec_sally`. All six logins were tested from the already-allowed network and can read the SEC tables/views without shared-data write or schema-creation permissions. Credentials are shared privately.

AWS network configuration is complete for direct IPv4 connections. Successful logins from the teammates' own machines remain unverified; test each connection before marking onboarding complete. Reader accounts and their permissions are unchanged and do not need to be recreated. Michael retains shared writes initially; restricted loader permissions can be considered after local implementation and review.

For the maintainer to check live status from this checkout, after completing [local setup](developer-usage.md):

```powershell
.\.venv\Scripts\python.exe -m sec_pipeline.db_cli --env-file .env.aws status
```

## 1. Sign in and check billing

Open the [AWS console](https://console.aws.amazon.com/console/home). Complete sign-in or account creation directly in AWS. Keep passwords, payment information, and verification codes out of chat and tracked files.

In Billing and Cost Management, check the account plan, Free Tier status, credit balance, and expiration dates. Set a monthly cost budget with email alerts before creating the database; a budget alert does not cap spending. Free-plan credits and eligibility are account-specific. Sharing PostgreSQL credentials does not require inviting teams into AWS Organizations.

## 2. Review the instance configuration

For provisioning or recovery, use RDS **Standard create** so the settings are visible. Select a Free tier template if the account offers it; otherwise use the development/sandbox option and review every value. Console labels may differ by account plan. The table retains the initial instance baseline and includes the October 7 network access change.

| Setting | Configuration |
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
| Public access | Yes, for direct laptop connections |
| Security group | `sec-filings-access`: PostgreSQL TCP 5432 from `0.0.0.0/0` (Anywhere-IPv4) |
| Credentials | Self managed for this starter estimate; enter and retain the password directly in a password manager |
| Automated backups | 1 day for the Free plan; use longer retention only if the account plan allows it |
| Deletion protection | Enabled |
| Monitoring | Basic/standard options; review charges before enabling additional monitoring |

Public accessibility provides the endpoint, and the current security group permits incoming IPv4 connections on TCP 5432 without individual IP rules. Database authentication, read-only role permissions, and verified TLS remain part of the connection setup. This setting does not open other ports or add IPv6 access. A later move to restricted IPs or a private network would require updating these connection instructions.

The October 6 planning estimate for 730 running hours in `us-east-1` was approximately $11.68 compute + $2.30 storage + $3.65 for one public IPv4 address = **$17.63/month before credits**. This is a dated baseline, not a cap or a current quote. Taxes, CPU bursting, additional backup storage, traffic, and optional services can add charges. Review AWS's current pricing and creation summary before submission. No NAT gateway, VPN, EC2 instance, proxy, or paid support is included in this estimate.

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

This procedure is for a new, empty destination during deployment or recovery. It is not a refresh procedure for the populated shared RDS database.

Start Docker Desktop and the existing local database. Use the [backup workflow](database.md#backup) to produce a fresh, uniquely named custom-format dump after confirming local status. Preserve all existing backups.

Scope the fresh archive to **schema `sec`** using `pg_dump --schema=sec -Fc`, then restore that entire scoped archive into the new, empty RDS database using PostgreSQL 17 `pg_restore` and verified TLS. Use `--no-owner --no-privileges --exit-on-error --single-transaction`. Omit `--schema` from the restore command so the archive's `CREATE SCHEMA` entry is included. The local administrator role and grants should not be copied into RDS. The existing Docker container supplies a compatible client if none is installed on Windows. Copy the dump and CA bundle into that container before restoring from it, and use the corresponding container paths.

Supply passwords using an interactive password prompt or a private credential mechanism, never a command argument or connection URL. Do not use `--clean`, overwrite another populated database, or seed before restoring. Dump/restore preserves existing UUIDs, provenance, processed records, and the migration ledger.

For a new restore, run the maintainer migration and status commands:

```powershell
.\.venv\Scripts\python.exe -m sec_pipeline.db_cli --env-file .env.aws migrate
.\.venv\Scripts\python.exe -m sec_pipeline.db_cli --env-file .env.aws status
```

For a current-schema dump, `migrate` should apply nothing and validate the existing migration hashes; if migrations are pending, this command applies them. Compare all table counts, record IDs, and citation views against the source used for that dump. The initial metadata-only import contained one company, one filing, four source documents, and no processed content. The shared RDS database now also contains the manual sample, so use fresh counts from the actual source and destination for a new restore. Check `pg_stat_ssl` for the active connection and test reconnecting.

The source files and `source_manifest.json` are not contained in the database. Retain matching local copies for each loader until shared object storage and its file-resolution support are implemented.

## 5. Share controlled access

The six current teammate logins already belong to the read-only `sec_reader` group. Reuse these accounts for onboarding. For future people or services, create separate logins and assign appropriate group roles. Reserve schema migrations, role administration, and database ownership for Michael. Keep shared imports with Michael until a reviewed loader needs a restricted write role; scope any grant to its actual tables and operations, without schema administration or overwriting source provenance.

Each team needs the endpoint, port, database name, its own login, the CA bundle, and instructions to use `verify-full`. Share credentials privately. Under the current Anywhere-IPv4 rule, teammates do not need to submit public IP addresses. Verify login and a reader query from each teammate's machine before calling onboarding complete; local success alone does not prove another team's network can connect.

## References

- [RDS creation settings](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_CreateDBInstance.html)
- [RDS networking](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_VPC.WorkingWithRDSInstanceinaVPC.html)
- [RDS certificate bundles](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html)
- [Regional RDS price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/us-east-1/index.json)
- [Public IPv4 pricing](https://aws.amazon.com/vpc/pricing/)
- [Free Tier rules](https://aws.amazon.com/free/free-tier-faqs/)
