# Remote MariaDB and Ollama

This release supports explicit remote addresses. Chrome still runs visibly on
YOUR Windows PC, with manual SEEK sign-in. MariaDB and Ollama can be on different
servers. No remote server has been configured or contacted on your behalf.

## Install and configure

Stop the old run. Copy all eight application files from this package into your
existing folder: `compare.py`, `repository.py`, `profiles.py`, `seek_browser.py`,
`runtime_breaks.py`, `network_config.py`, `review_schema.py`, and `work_claims.py`. Also copy
`review_schema.sql` next to them. Keep your reports and existing
`config.json`. In your activated Python environment:

```powershell
pip install -r requirements.txt
Copy-Item config.remote.example.json config.remote.json
notepad config.remote.json
```

Replace the example values with your actual settings:

| Setting | Value to enter |
| --- | --- |
| `database.host` | MariaDB server IP or DNS name, without `http://` |
| `database.port` | MariaDB port, usually 3306 |
| `database.user` | Account permitted to connect from your PC |
| `database.database` | Exact database name; `trackitlive` is only an example |
| `database.target_table` | Keep `seek_scrap_detail` |
| `ollama.endpoint` | Ollama base URL, such as `http://YOUR-OLLAMA-SERVER:11434` |
| `ollama.model` | Model installed on that server, such as `llama3.1:8b` |
| `created_by` | Staff ID running the collector |
| `review.assigned_reviewer` | Staff ID assigned to review, or null for unassigned |

The example hostnames ending in `.example` are placeholders. The four idle
settings are read from `seek_scrap_settings` in THIS SAME configured database;
`breaks.settings_id` defaults to 1. Ten-second browser request delays remain.

The program prompts for the MariaDB password, or reads `SEEK_DB_PASSWORD` from
your environment. It does not read a password from JSON. Keep actual configs,
passwords, certificates and reports out of Git. The included ignore rules cover
the example workflow; files already tracked by Git remain tracked.

## Check before running

Put `--config` BEFORE the subcommand in every command:

```powershell
python compare.py --config config.remote.json check-connections
```

This checks the selected database, read-only source columns, review queue/history and work-claim tables,
break settings and whether Ollama lists your model. It does not open Chrome,
send candidate profiles, generate model responses or write database records.
An available-model check does not guarantee the server has enough memory for
inference. `--no-ollama` on this command checks MariaDB only.

Run setup once after this update to create missing review/work-claim tables:

```powershell
python compare.py --config config.remote.json init-db
python compare.py --config config.remote.json check-connections
```

`init-db` creates `seek_uuid_match_review`, `seek_uuid_match_review_history`
and `seek_uuid_work_claim` in the configured database. Routine collection
needs SELECT on candidate/settings tables and SELECT/INSERT on the two review
tables, plus SELECT/INSERT/UPDATE on `seek_uuid_work_claim`. Setup also needs CREATE. No candidate UPDATE or ALTER grants are needed.

Numeric IDs are read from `seek_scrap_detail.seekid_detail` by default.
People are ordered by their latest `seek_scrap.date_updated` descending, joined
through `seek_scrap.seek_scrap_id = seek_scrap_detail.id_detail`; the collector
also needs SELECT on `seek_scrap`. The latest schema stores the existing UUID in
`seek_scrap_detail.uuid`; `seek_scrap.seek_scrap_id` is an integer link in the
main table. Python never changes either field. MariaDB 10.1.48 is identified in
the supplied dump; see SCHEMA_COMPATIBILITY.md for the version-gated setup SQL.
The old `prepare-detail` and `rollback` commands have been removed.
`setup_local_scrap_settings.sql` targets the LOCAL test database and is not a
remote migration. The selected remote settings row must exist.

## Trial, then submit for review

One candidate, using remote Ollama, WITHOUT database writes:

```powershell
python compare.py --config config.remote.json run --limit 1 --auto-propose --with-ollama
```

Five candidates, submitting qualifying matches as pending reviews:

```powershell
python compare.py --config config.remote.json run --limit 5 --submit --auto-propose --with-ollama
```

After checking the pending review records, process up to one million pending numeric IDs:

```powershell
python compare.py --config config.remote.json run --limit 1000000 --submit --auto-propose --with-ollama
```

Automatic proposal selection still requires identical normalized COMPLETE Profile content.
Names, similarity scores and Ollama opinions cannot replace that rule. The first
qualifying comparison is submitted with status pending and the scraper moves to the next numeric ID.
`--with-ollama` additionally sends the extracted old/new profiles to your chosen
Ollama server for an advisory opinion. It adds inference time. Human approval happens only in the separate app. Without this flag,
`--auto-propose` skips Ollama; it still uses your remote MariaDB. Collection without --auto-propose
uses Ollama by default unless `--no-ollama` is supplied.

## Network and TLS

From your Windows PC, substitute actual server names:

```powershell
Test-NetConnection YOUR-MARIADB-SERVER -Port 3306
Test-NetConnection YOUR-OLLAMA-SERVER -Port 11434
```

The MariaDB server must listen on a reachable interface and allow the configured
account from your PC. Network/firewall access must allow the selected ports.
Ollama's listening address is configured on its server with `OLLAMA_HOST`; restart
Ollama after changing it. For example, bind to the server's private IP and port
11434. See the [official Ollama server configuration guide](https://docs.ollama.com/faq).

The sample disables database TLS and uses HTTP for a trusted private LAN/VPN.
For MariaDB TLS, replace the `database.tls` object with:

```json
{"enabled": true, "ca_file": "C:/certs/mariadb-ca.pem"}
```

The host must match the server certificate. Certificate and hostname verification
remain enabled, and an unencrypted session is rejected. Omit `ca_file` to use
system-trusted certificates. If the server requires a client certificate, also
supply `cert_file` and `key_file`. These options use PyMySQL's SSL context support:
[connection documentation](https://pymysql.readthedocs.io/en/latest/modules/connections.html).

For an HTTPS Ollama gateway, set `ollama.endpoint` to its final HTTPS base URL.
An internal CA can be supplied as `ollama.ca_file`. If your gateway requires a
Bearer token, add `"api_key_env": "SEEK_OLLAMA_API_KEY"` and set that environment
variable outside the config file. Tokens require HTTPS; redirects are rejected.
This does not add authentication to the server. Self-hosted Ollama's API does
not require authentication by default, so keep it private or use an authenticated
HTTPS gateway: [Ollama authentication documentation](https://docs.ollama.com/api/authentication).

Troubleshooting: connection refused/timeout means check address, listener and
firewall; database access denied means check account, password and allowed client
host; missing model means install/select the model on the REMOTE Ollama server.
If a long database pause occurs, the connection is refreshed outside the write
transaction before reading settings again.

For simultaneous workers, update every machine and use the same shared database
and target_table. Work claims are automatic with --submit; see MULTI_MACHINE.md.
