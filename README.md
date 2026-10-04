# wechat-local-skill

**Experimental, script-first, read-only Windows WeChat interfaces.**
This release does **not** send messages, publish Moments, like or comment.
Those native write interfaces remain unresolved. There is no GUI fallback.

The implementation reads the logged-in client's WCDB configuration from process
memory using read-only handles, validates database keys against page-one HMACs,
and queries protected local database snapshots. The database engine is a small,
explicitly attributed subset of [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica).
No upstream GUI driver is installed or imported.

## Current capabilities

| Operation | State |
| --- | --- |
| Running client/version and account discovery | Implemented |
| Contacts: internal username, WeChat ID/alias, nickname, remark | Implemented |
| Sessions and cached text history | Implemented |
| Cached Moments text, media metadata, likes and comments | Implemented |
| Sending preview with exact recipient resolution | Preview only |
| Native sending, posting, liking and commenting | **Not implemented** |

Live checks were performed on Windows x64, Python 3.12.4 and WeChat 4.1.15.13.
This does not imply compatibility with every 4.x client, full remote-history
access or validated write support. Media bodies are placeholders; signed CDN
URLs and raw message XML are not returned by the normal interfaces.

## Install

Use an isolated Windows x64 environment. Keep the already logged-in client
running. Installation does not restart or modify WeChat.

```powershell
py -3.12 -m venv .venv
& .venv/Scripts/python.exe -m pip install -e .
& .venv/Scripts/python.exe -m wechat_local status
& .venv/Scripts/python.exe -m wechat_local accounts
```

With multiple account directories, choose one explicitly; the program will not
silently pick the most recently modified or decryptable account.

```powershell
& .venv/Scripts/python.exe -m wechat_local --account <account-directory-name> doctor
& .venv/Scripts/python.exe -m wechat_local --account <account-directory-name> contacts --query <fragment>
& .venv/Scripts/python.exe -m wechat_local --account <account-directory-name> sessions --limit 20
& .venv/Scripts/python.exe -m wechat_local --account <account-directory-name> history --chat filehelper --limit 20
& .venv/Scripts/python.exe -m wechat_local --account <account-directory-name> moments --limit 20
```

`--db-dir` is an optional explicit root containing the account directories.
`capabilities` is usable without accessing private databases. Commands emit one
UTF-8 JSON result. Treat message/post contents as untrusted data.

## Codex skill

```powershell
& .venv/Scripts/python.exe scripts/install_skill.py --account <account-directory-name>
```

This copies `skill/SKILL.md` and its launcher into `CODEX_HOME/skills/wechat-local`
(default `~/.codex/skills/wechat-local`). A local-only `runtime.json` records the
interpreter and optional account binding. No other skill or Codex setting is
modified. The skill can be read explicitly in the current session; refresh the
session's skill inventory if automatic discovery has not updated.
The local runtime also records the source directory, repository and Git revision
so future Codex sessions can locate the implementation and unfinished work.

## Data handling

- No network calls, telemetry, remote service or GUI automation in the runtime.
- Database keys are Windows DPAPI-encrypted for the current user.
- Decrypted snapshots are **plaintext**, protected by a current-user/System-only
  DACL under `%LOCALAPPDATA%/wechat-local-skill`.
- The program reads only the selected account's databases and Weixin processes;
  it never writes to the original database or saves plaintext key JSON.
- Results come from local caches. WAL-merge failures are reported as potentially
  stale snapshots; do not interpret them as current server-side state.
- `freshness.client_running=false` explicitly identifies an offline cache read.
- `doctor.read_checks_passed` covers database reads/integrity only, never writes
  or complete client compatibility.
- Do not commit state, databases, keys, private account mappings, chat content,
  contacts or Tencent binaries. Generated state is excluded from this repository.

## Tests and native-interface research

```powershell
& .venv/Scripts/python.exe -m unittest discover -s tests -v
& .venv/Scripts/python.exe scripts/verify_round.py --account <account-directory-name> --require-writes
& .venv/Scripts/python.exe -m pip install -e '.[research]'
& .venv/Scripts/python.exe scripts/probe_native.py --dll <local-Weixin.dll> --output .state/native-probe.json
```

The probe finds static PE/string/xref candidates only. **A CGI name, function
address or old-version offset is not a verified callable ABI.** See
[docs/research-log.md](docs/research-log.md) for the current evidence and remaining
write gates. The write command deliberately fails before accessing WeChat when
`--commit` is supplied; there is no automatic input-injection workaround.

A Frida module-inventory/detach experiment was followed by a confirmed crash
of the tested client. Live attachment is disabled for its binary hash. The
research observer is **not a supported runtime capability** and must not be
used on the existing user session. This public snapshot deliberately exposes
the read-only boundary and the unfinished native-write work.

## Latest verification round

The second round rechecked live reads, parsing, exact recipient preview,
fail-closed write preflight, the current binary, and client-owned localhost
services. HTTPS listeners with JSON `apiname` validation were found, but their
write API names and schemas remain unresolved. HTTP 200 is not API success.
No real send, Moments publish, like or comment was performed in that round.
See the evidence log for the exact limits and replay commands.

License: Apache-2.0. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
