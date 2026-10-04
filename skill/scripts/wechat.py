"""Launch the installed local interface with its explicit account binding."""
import json
from pathlib import Path
import subprocess
import sys


def main():
    runtime_path = Path(__file__).resolve().parents[1] / "runtime.json"
    if not runtime_path.is_file():
        raise SystemExit("Skill runtime is not configured; run scripts/install_skill.py first")
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    globals_ = []
    for flag, field in (("--account", "account"), ("--db-dir", "db_dir")):
        if runtime.get(field) and flag not in sys.argv[1:]:
            globals_.extend([flag, runtime[field]])
    result = subprocess.run([runtime["python"], "-X", "utf8", "-m", "wechat_local",
                             *globals_, *sys.argv[1:]], check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
