# Runtime data

Everything in this directory except this README is gitignored. JobPilot creates
it on demand; a fresh install starts with no profile, jobs or applications.

```text
data/
  auth.db                      shared account records (emails + password hashes)
  users/
    usr_<id>/
      jobpilot.db              profile, jobs, chats, memories, applications, usage
      traces/                  JSONL run traces
      uploads/                 generated CVs and cover letters
```

The web app resolves the signed-in account and uses that account's workspace.
`JOBPILOT_DB_PATH`, `JOBPILOT_TRACES_DIR` and `JOBPILOT_UPLOADS_DIR` are base
paths for CLI and test runs; they are redirected per account inside the app.

## Reset

Stop the server first. To remove every account and all workspace data:

```powershell
Remove-Item -Recurse -Force data\users -ErrorAction SilentlyContinue
Remove-Item -Force data\auth.db, data\auth.db-shm, data\auth.db-wal -ErrorAction SilentlyContinue
```

```bash
rm -rf data/users
rm -f data/auth.db data/auth.db-shm data/auth.db-wal
```

To reset one account, delete only its `data/users/<user_id>/` directory, then
sign in again. Its profile and job history will start blank; the login itself
stays in `data/auth.db`.
