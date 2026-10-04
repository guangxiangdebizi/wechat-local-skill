# Third-party source

`src/wechat_local/vendor/replica_db.py` is derived from `wechatauto/db.py`
in [fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica),
revision `91dc0e1a601013b759b261061d8f7642f736af90` (upstream 1.2.4.4).
The upstream license is Apache-2.0; see `licenses/wechatauto-replica.txt`.
The only vendored-source edit is the logger import. Privacy and account-selection
changes are implemented in a separate subclass, not silently patched upstream.

No upstream GUI, OCR, mouse/keyboard, accessibility-patching or sending driver
is included or imported. No Tencent executable or database is distributed.

The native write-interface investigation references
[aixed/WeChat-Hook](https://github.com/aixed/WeChat-Hook), whose documented target
is WeChat 4.1.10.27. Its offsets are not a validated profile for 4.1.15.13.
No code from that repository is incorporated into the runtime.
