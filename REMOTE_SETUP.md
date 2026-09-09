# Remote MariaDB and Ollama

This release supports explicit remote addresses. Chrome still runs visibly on
YOUR Windows PC, with manual SEEK sign-in. MariaDB and Ollama can be on different
servers. No remote server has been configured or contacted on your behalf.

## Install and configure

Stop the old run. Copy all six application files from this package into your
existing folder: `compare.py`, `repository.py`, `profiles.py`, `seek_browser.py`,
`runtime_breaks.py`, and `network_config.py`. Keep your reports and existing
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
| `reviewer` | Your staff ID |

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

This checks the selected database, destination column, mapping/audit tables,
break settings and whether Ollama lists your model. It does not open Chrome,
send candidate profiles, generate model responses or write database records.
An available-model check does not guarantee the server has enough memory for
inference. `--no-ollama` on this command checks MariaDB only.

If only the mapping/audit tables are missing, create them explicitly:

```powershell
python compare.py --config config.remote.json init-db
python compare.py --config config.remote.json check-connections
```

`init-db` creates `seek_candidate_identity_map` and `seek_uuid_backfill_audit`
in the configured database. The account needs CREATE for initialization and
SELECT/INSERT/UPDATE/DELETE for mapping/audit operations, SELECT/UPDATE on the
chosen target, and SELECT on `seek_scrap_settings`.

The UUID destination remains `seek_scrap_detail.seek_scrap_id`, selected using
numeric `seekid_detail`. It must be a text column large enough for 36 characters.
Existing nonblank values are preserved, including old numeric parent links.
If the remote schema still uses this column as an integer relationship, coordinate
the schema and consuming VB.NET code before repurposing it. `prepare-detail`
explicitly alters/seeds the CONFIGURED database; it is never run automatically.
`setup_local_scrap_settings.sql` targets the LOCAL test database and is not a
remote setup script. The remote database must contain the chosen settings row.

## Trial, then save

One candidate, using remote Ollama, WITHOUT database writes:

```powershell
python compare.py --config config.remote.json run --limit 1 --auto-save --with-ollama
```

Five candidates, saving qualifying matches automatically:

```powershell
python compare.py --config config.remote.json run --limit 5 --apply --auto-save --with-ollama
```

After checking the saved results, process up to one million pending numeric IDs:

```powershell
python compare.py --config config.remote.json run --limit 1000000 --apply --auto-save --with-ollama
```

Automatic saving still requires identical normalized COMPLETE Profile content.
Names, similarity scores and Ollama opinions cannot replace that rule. The first
qualifying match is saved and the scraper moves to the next numeric ID.
`--with-ollama` additionally sends the extracted old/new profiles to your chosen
Ollama server for an advisory opinion. It adds inference time. Without this flag,
`--auto-save` skips Ollama; it still uses your remote MariaDB. Manual review mode
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
