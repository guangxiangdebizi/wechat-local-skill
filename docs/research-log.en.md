# Evidence log

## 2026-10-04: initial read-only implementation

### Sources and boundaries

- Database reader: `fanyuantaier/wechatauto-replica`, revision
  `91dc0e1a601013b759b261061d8f7642f736af90`, Apache-2.0.
  Only the database module is vendored. Logger import is the sole source edit;
  separate code handles explicit account selection and private DPAPI key storage.
- Native-send reference inspected: `aixed/WeChat-Hook`, revision
  `e905d07ade50d2c6472e4eb3bd4f3fe19cf662c6`, documented target 4.1.10.27.
  No hook DLL, restart, injection or stale-offset call was attempted.
- Official wxauto installation documentation lists the free version's upper
  client limit as 4.1.8.107. The current client is newer. The open skill wrapper
  does not make its wxautox4 activation dependency free or open source.

### Observations

- Active client: WeChat/Weixin 4.1.15.13, Windows x64, Python 3.12.4 x64.
- Local `Weixin.dll`: size 201,552,944 bytes; SHA-256
  `10f8e995453e2da46d4f2b5080cd6da1f13cc5147746adc119ceae38cb039de5`.
- Explicit account binding passed the page-one key HMAC checks. Contact,
  session and SNS snapshots passed `PRAGMA quick_check`. Text-history and
  Moments fields were read successfully from the live local snapshots.
- A first Moments parser handled XML syntactically but looked for fields at
  the wrong depth: live rows use `SnsDataItem/TimelineObject`. Empty output
  was not accepted as success. The parser was corrected, covered by a
  nested-container/CDATA/deleted-comment fixture, and rechecked on live rows.
- All initial offline tests pass. No messages, posts, likes or comments were
  created. No private content or account identifiers are included in this log.
- The observed localhost listeners did not respond to a standard read-only
  `/json/version` request. This is not evidence that all IPC routes are absent.
- Static strings include `newsendmsg`, `mmsnspost`, `mmsnscomment`, and matching
  protobuf schema names. These are protocol/research anchors, not externally
  callable authenticated endpoints.
- The reference's `create_param2` (RVA `0xdf40`), `send_message` (`0x1677a30`)
  and text constructor (`0x6b2c30`) are not function-start RVAs in the current
  image's unwind metadata. Their current bytes do not represent a portable
  native-send profile. Do not execute these old offsets.

### Remaining work, not completed results

1. Resolve current native message construction/dispatch and the required
   account/service context, calling convention, thread affinity and ownership.
2. Capture one narrowly authorized normal-send trace, validate all argument
   layouts and prove the path on File Transfer Assistant before other recipients.
3. Require an exact version/hash profile, per-operation idempotency and local
   outgoing-record confirmation. Never claim remote delivery from a return code.
4. Independently resolve and test Moments publishing, likes and comments;
   message sending does not establish any of those capabilities.
5. Keep private runtime traces/state separate from source. Publish only verified
   support claims; until those gates pass this project remains read-only.

### Replay

See README commands for isolated installation, tests, `status`, account-bound
`doctor` and static probing. For live read checks, use the locally selected
account and retain only aggregate verification status in shareable reports.
Keep the original databases untouched. A valid cached key round trip or unit
test alone does not validate native writing or full-client compatibility.

## 2026-10-04: Frida inventory failure; live attachment disabled

- The user requested withholding GitHub publication until chat/Moments reads
  **and writes** are completed. No GitHub repository was created or pushed.
- Read-only source was installed as a local, explicitly partial skill. Its
  launcher and validation work; it does not implement or attempt live writes.
- A single Frida 17.22.1 attachment to the confirmed Weixin main process loaded
  a script that only enumerated its own module metadata. The script reported
  x64, `Weixin.dll`, loaded-image size 202,039,296 and **zero interceptors**.
  No `NativeFunction`, message send, API replay, argument dump or hook was run.
- After script unload/session detach, that main process and its child processes
  disappeared. A subsequent observation command stopped immediately with
  `NoSuchProcess`; it never attached or installed the prepared interceptors.
- Windows Application events 1000 and 1001 confirm an APPCRASH of the same
  main-process PID. Faulting module: `ntdll.dll`; exception `0xc0000005`;
  fault-module offset `0x165497`. This establishes the crash and temporal
  relationship, not a proven root cause or an anti-instrumentation mechanism.
- The corresponding binary hash is blocked in `observe_native.py`. No retry,
  automatic restart, login automation, driver/security-setting change or
  alternative live injection was performed. No test message was sent.
- Preserve this negative evidence. Static message/Moments anchors and passing
  read tests do **not** make native writes ready. Continue offline analysis;
  any future live experiment needs a new explicit user decision and an
  appropriately isolated process, rather than reusing the affected session.

## 2026-10-04: user-restarted client recovery check

- The user manually reopened WeChat. Passive process discovery confirmed a
  new main process and four child processes, still version 4.1.15.13.
- The installed skill's `status` and account-bound `doctor` commands succeeded.
  All nine selected database keys still passed validation; contact, session and
  SNS snapshots passed `PRAGMA quick_check`. Cached text history was readable.
- The SNS cache contained newly synced rows, and `freshness.client_running`
  returned true with no recorded WAL-merge failure. This demonstrates read
  recovery, not a validated sender or complete server-side data access.
- No Frida attachment, interception, native function call or message send was
  attempted against the reopened process. The known-binary live-observer block
  remains in force. GitHub publication remains deferred as requested.

## 2026-10-05: second round and authorized partial publication

- The user requested one further verification round, and explicitly authorized
  publishing the current partial source if native writes still could not be
  completed. This supersedes the earlier publication deferral; it does not
  make unfinished write capabilities supported.
- Live process/version and database-integrity checks, contacts, sessions,
  File Transfer Assistant history and nested Moments parsing passed again.
  Recipient preview remained a dry run. `send --commit` was rejected before
  a database/process write could occur. Full read/write verification did not
  pass; no real outbound write or delivery test was performed.
- The binary hash and 22 static anchored functions were rechecked. Native
  argument layouts/service context are still unresolved. No old offset was
  executed, and the previously crashing Frida path was not retried.
- New observation: client-owned loopback ports 14013 and 14016 serve **HTTPS**.
  A GET to `/json/version` gets HTTP 200 with an application error, not a CDP
  version response. The earlier plain-HTTP failure did not establish that
  there was no accessible local service.
- Port 14016 parses a JSON POST body's `apiname`. A synthetic unknown name is
  echoed with `errcode=-11028`, `errmsg="invalid apiname"`; the same value in a
  GET query is not selected. Four metadata-only candidate names (`getVersion`,
  `getClientVersion`, `getLoginStatus`, `getApiList`) were rejected identically.
  Port 14013 returned error 10057. No write payload was submitted to either.
- Exact UTF-8/UTF-16 localization of `invalid apiname` in the main image,
  observed client DLLs and main-process readable memory produced no usable
  registration table. This limited search does not prove that the table or
  write interfaces do not exist; child-process dispatch/resources remain
  plausible explanations. API names, schema and authorization remain gates.
- The new verification helper distinguishes passing read/guard checks from
  unresolved writes. `--require-writes` returns failure, even when all read
  checks pass. A preview that reports a send fails its own regression test.
- The current source will be published as an experimental read-only snapshot,
  with these failures and replay scripts retained. No chat text, contacts,
  account mappings, keys, databases, runtime traces or Tencent binaries are
  part of the publication.

### Second-round replay

```powershell
python -X utf8 scripts/verify_round.py --account <selected-account> --require-writes
python -X utf8 scripts/probe_native.py --dll <loaded-Weixin.dll> --output .state/native-probe.json
python -X utf8 scripts/probe_local_services.py --pid <confirmed-Weixin-main-PID>
python -X utf8 scripts/probe_runtime_labels.py --pid <confirmed-Weixin-main-PID>
```

Do not run the live observer against the blocked binary. Keep actual account
bindings and process IDs local. Unit/build/skill checks are separate from
native write validation, which remains unfinished.
