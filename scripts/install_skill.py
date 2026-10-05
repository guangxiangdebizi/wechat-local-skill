"""Install only this skill's files; never change other skills or Codex settings."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="安装或更新本项目的 Codex skill，不修改其他技能和配置")
    parser.add_argument("--account", help="明确绑定的微信账号目录名")
    parser.add_argument("--db-dir", help="包含账号目录的微信数据根目录")
    parser.add_argument("--codex-home", type=Path, help="指定 Codex 用户目录")
    parser.add_argument("--update", action="store_true", help="显式更新已存在的本项目 skill")
    args = parser.parse_args()
    if importlib.util.find_spec("wechat_local") is None:
        raise SystemExit("Install the local package into this interpreter before installing the skill")
    root = Path(__file__).resolve().parents[1]
    source = root / "skill"
    codex_home = args.codex_home or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    target = codex_home / "skills" / "wechat-local"
    if target.exists() and not args.update:
        raise SystemExit("Skill already exists; inspect it first and use --update explicitly")
    revision = None
    if (root / ".git").exists() and shutil.which("git"):
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            revision = result.stdout.strip()
    runtime = {"python": sys.executable, "package": "wechat_local", "account": args.account,
               "db_dir": args.db_dir, "source_root": str(root), "git_commit": revision,
               "repository": "https://github.com/guangxiangdebizi/wechat-local-skill",
               "release_boundary": "default_read_only_opt_in_uia_text_and_image_send"}
    target.mkdir(parents=True, exist_ok=True)
    (target / "scripts").mkdir(exist_ok=True)
    shutil.copy2(source / "SKILL.md", target / "SKILL.md")
    shutil.copy2(source / "scripts" / "wechat.py", target / "scripts" / "wechat.py")
    (target / "runtime.json").write_text(json.dumps(runtime, indent=2), encoding="utf-8")
    print(json.dumps({"installed": True, "skill": "wechat-local", "path": str(target),
                      "release_boundary": "default_read_only_opt_in_uia_text_and_image_send"}))


if __name__ == "__main__":
    main()
