import argparse
import json
import platform
import re
import shlex
import subprocess
import sys
import requests
from pathlib import Path

ROOT = Path(".minecraft")
MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


def ensure_version_json(version_id):
    version_dir = ROOT / "versions" / version_id
    json_path = version_dir / f"{version_id}.json"
    if json_path.exists():
        return json.loads(json_path.read_text(encoding="utf-8"))

    manifest = requests.get(MANIFEST, timeout=30).json()
    entry = next((v for v in manifest["versions"] if v["id"] == version_id), None)
    if not entry:
        raise SystemExit(f"版本不存在: {version_id}")

    vjson = requests.get(entry["url"], timeout=60).json()
    version_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(vjson, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已补写 {json_path}")
    return vjson


def current_os_name():
    return {"Windows": "windows", "Linux": "linux", "Darwin": "osx"}.get(
        platform.system(), platform.system().lower())


def is_32bit():
    machine = platform.machine().lower()
    return "32" in machine or machine in ("i386", "i686")


def rule_allows(rule, os_name):
    if "os" in rule:
        os_info = rule["os"]
        if os_info.get("name") and os_info["name"] != os_name:
            return False
        if os_info.get("arch") == "x86" and not is_32bit():
            return False
        if os_info.get("version") and os_name == "windows":
            if not re.search(os_info["version"], platform.version()):
                return False
    if "features" in rule:
        if any(rule["features"].values()):
            return False
    return rule.get("action") == "allow"


def filter_arguments(arg_list, os_name):
    result = []
    for item in arg_list:
        if isinstance(item, str):
            result.append(item)
            continue
        rules = item.get("rules")
        if rules and not any(rule_allows(r, os_name) for r in rules):
            continue
        value = item.get("value")
        if isinstance(value, list):
            result.extend(value)
        else:
            result.append(value)
    return result


def build_classpath(version_id, vjson):
    os_name = current_os_name()
    sep = ";" if os_name == "windows" else ":"
    libs_dir = ROOT / "libraries"
    entries = []

    for lib in vjson.get("libraries", []):
        rules = lib.get("rules")
        if rules and not any(rule_allows(r, os_name) for r in rules):
            continue
        artifact = lib.get("downloads", {}).get("artifact")
        if not artifact:
            continue
        entries.append(str((libs_dir / artifact["path"]).resolve()))

    client_jar = (ROOT / "versions" / version_id / f"{version_id}.jar").resolve()
    entries.append(str(client_jar))
    return sep.join(entries)


def substitute(template_args, variables):
    result = []
    for arg in template_args:
        def repl(match):
            key = match.group(1)
            return str(variables.get(key, match.group(0)))
        result.append(re.sub(r"\$\{([^}]+)\}", repl, arg))
    return result


def detect_java_major(java="java"):
    try:
        out = subprocess.check_output(
            [java, "-version"], stderr=subprocess.STDOUT, text=True, timeout=10)
        m = re.search(r'version "(\d+)(?:\.(\d+))?', out)
        if not m:
            return None
        major = int(m.group(1))
        if major == 1 and m.group(2):
            return int(m.group(2))
        return major
    except Exception:
        return None


def required_java_major(version_id):
    try:
        parts = re.match(r"(\d+)\.(\d+)", version_id)
        if not parts:
            return 21
        minor = int(parts.group(2))
        if minor >= 20:
            return 21
        if minor >= 18:
            return 17
        if minor >= 17:
            return 16
        return 8
    except Exception:
        return 21


def generate_launch_command(version_id, username="Steve", java="java"):
    vjson = ensure_version_json(version_id)
    os_name = current_os_name()
    version_dir = ROOT / "versions" / version_id

    args = vjson.get("arguments", {})
    if not args and "minecraftArguments" in vjson:
        game_template = shlex.split(vjson["minecraftArguments"])
        jvm_template = []
        legacy = True
    else:
        jvm_template = filter_arguments(args.get("jvm", []), os_name)
        game_template = filter_arguments(args.get("game", []), os_name)
        legacy = False

    classpath = build_classpath(version_id, vjson)
    natives_dir = (version_dir / "natives").resolve()

    # 版本隔离：游戏目录指向 versions/<版本>/
    game_dir = version_dir.resolve()

    assets_dir = (ROOT / "assets").resolve()
    assets_index = vjson.get("assets", vjson.get("assetIndex", {}).get("id", ""))

    variables = {
        "natives_directory": str(natives_dir),
        "classpath": classpath,
        "classpath_separator": ";" if os_name == "windows" else ":",
        "launcher_name": "custom-launcher",
        "launcher_version": "1.0",
        "auth_player_name": username,
        "version_name": version_id,
        "game_directory": str(game_dir),
        "assets_root": str(assets_dir),
        "assets_index_name": assets_index,
        "auth_uuid": "00000000000000000000000000000000",
        "auth_access_token": "0",
        "clientid": "0",
        "auth_xuid": "0",
        "user_type": "msa",
        "version_type": vjson.get("type", "release"),
        "user_properties": "{}",
    }

    jvm_args = substitute(jvm_template, variables)
    game_args = substitute(game_template, variables)
    main_class = vjson.get("mainClass", "net.minecraft.client.main.Main")

    if legacy:
        cmd = [java] + ["-cp", classpath, main_class] + game_args
    else:
        cmd = [java] + jvm_args + [main_class] + game_args

    cmd = [a.replace("_javaw.exe_", "") for a in cmd]
    return cmd, game_dir


def launch(cmd, game_dir, dry_run=False):
    if dry_run:
        print(subprocess.list2cmdline(cmd))
        return 0

    print("启动命令:")
    preview = subprocess.list2cmdline(cmd)
    print(preview[:300] + (" ..." if len(preview) > 300 else ""))
    print(f"\n游戏目录: {game_dir}\n")

    try:
        proc = subprocess.Popen(cmd, cwd=str(game_dir))
    except FileNotFoundError:
        print("❌ 找不到 java 可执行文件，请用 --java 指定完整路径")
        return 1

    try:
        return proc.wait()
    except KeyboardInterrupt:
        print("\n已中断，正在结束游戏进程 ...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 130


def main():
    parser = argparse.ArgumentParser(
        description="Minecraft 启动器（离线模式，版本隔离）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python3 b.py 26.3 --username Steve\n"
            "  python3 b.py 26.3 --username Steve --dry-run\n"
            '  python3 b.py 26.3 --java "C:\\Program Files\\Java\\jdk-21\\bin\\java.exe"\n'
        ),
    )
    parser.add_argument("version")
    parser.add_argument("--username", default="Steve")
    parser.add_argument("--java", default="java")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令，不启动")
    args = parser.parse_args()

    if not (ROOT / "versions" / args.version / f"{args.version}.jar").exists():
        print(f"❌ 缺少 {args.version}.jar，先运行 python3 a.py {args.version}")
        sys.exit(1)

    detected = detect_java_major(args.java)
    required = required_java_major(args.version)
    if detected is None:
        print(f"⚠ 无法检测 Java 版本（{args.java}），继续尝试启动")
    elif detected < required:
        print(f"❌ 当前 Java 是 {detected}，{args.version} 需要 Java {required}+")
        sys.exit(1)
    else:
        print(f"Java {detected}，满足 {args.version} 的要求（{required}+）")

    cmd, game_dir = generate_launch_command(args.version, args.username, args.java)
    code = launch(cmd, game_dir, dry_run=args.dry_run)
    sys.exit(code)


if __name__ == "__main__":
    main()
