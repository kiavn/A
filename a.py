import argparse
import http.client
import json
import os
import sys
import platform
import zipfile
import hashlib
import threading
import time
import random
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dataclasses import dataclass

MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
RESOURCES = "https://resources.download.minecraft.net"

MIRRORS = [
    ("https://piston-meta.mojang.com/", "https://bmclapi2.bangbang93.com/"),
    ("https://launchermeta.mojang.com/", "https://bmclapi2.bangbang93.com/"),
    ("https://launcher.mojang.com/", "https://bmclapi2.bangbang93.com/"),
    ("https://resources.download.minecraft.net/", "https://bmclapi2.bangbang93.com/assets/"),
]

ROOT = Path(".minecraft")
DEFAULT_THREADS = 4
MAX_THREADS = 64
DEFAULT_RETRIES = 20
MAX_RETRIES = 30

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 10

ASSET_THREAD_MULTIPLIER = 2
ASSET_THREAD_CAP = 16

# 版本隔离：这些目录按版本分开，存档、Mod、配置互不污染
ISOLATED_DIRS = [
    "saves",
    "mods",
    "config",
    "resourcepacks",
    "shaderpacks",
    "logs",
    "crash-reports",
    "screenshots",
]

_thread_local = threading.local()


def resolve_url(url):
    for official, mirror in MIRRORS:
        if url.startswith(official):
            return url.replace(official, mirror, 1)
    return url


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def classify_error(e):
    if isinstance(e, (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
        http.client.IncompleteRead,
        http.client.RemoteDisconnected,
    )):
        return "fast"
    return "slow"


def build_session(proxy):
    session = requests.Session()
    session.headers.update({"User-Agent": "mc-downloader/1.0"})
    session.trust_env = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=1,
        pool_maxsize=1,
        max_retries=0,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def worker_init(proxy):
    _thread_local.session = build_session(proxy)


def get_session():
    session = getattr(_thread_local, "session", None)
    if session is None:
        _thread_local.session = build_session(None)
    return _thread_local.session


def fetch_json_safe(session, url, retries=DEFAULT_RETRIES):
    last_err = None
    candidates = [resolve_url(url)]
    if url not in candidates:
        candidates.append(url)

    for idx, candidate in enumerate(candidates):
        attempts = 1 if idx == 0 and candidate != url else retries
        for i in range(attempts):
            try:
                r = session.get(
                    candidate,
                    timeout=(10 if idx == 0 else CONNECT_TIMEOUT,
                             20 if idx == 0 else READ_TIMEOUT * 2),
                )
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.exceptions.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err = e
                if i + 1 < attempts:
                    time.sleep(min(2 ** i, 10) + random.uniform(0, 0.5))
    raise last_err if last_err else RuntimeError(f"fetch failed: {url}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Minecraft 完整文件下载器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python a.py                         交互式选择（推荐）\n"
            "  python a.py 1.21 -t 4               指定版本和核心线程\n"
            "  python a.py 1.21 --retries 10       加大重试\n"
        ),
    )
    parser.add_argument("version", nargs="?", help="版本号；省略则交互选择")
    parser.add_argument("-t", "--threads", type=int, default=None,
                        help=f"核心文件并发线程数（默认 {DEFAULT_THREADS}），assets 自动 ×{ASSET_THREAD_MULTIPLIER}")
    parser.add_argument("--proxy", default=None,
                        help="仅非 TUN 场景使用，如 http://127.0.0.1:7890")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help=f"单文件最大重试次数（默认 {DEFAULT_RETRIES}）")
    return parser.parse_args()


def ask_threads(default=DEFAULT_THREADS):
    while True:
        raw = input(f"核心文件线程数（1-{MAX_THREADS}，直接回车用 {default}）: ").strip()
        if not raw:
            return default
        try:
            n = int(raw)
        except ValueError:
            print("请输入整数")
            continue
        if 1 <= n <= MAX_THREADS:
            return n
        print(f"范围是 1-{MAX_THREADS}")


def validate_threads(n):
    if not (1 <= n <= MAX_THREADS):
        raise SystemExit(f"线程数必须在 1-{MAX_THREADS} 之间，收到 {n}")
    return n


def validate_retries(n):
    if not (0 <= n <= MAX_RETRIES):
        raise SystemExit(f"重试次数必须在 0-{MAX_RETRIES} 之间，收到 {n}")
    return n


class ProgressBar:
    def __init__(self, phase=""):
        self.lock = threading.Lock()
        self.items = {}
        self._stop = threading.Event()
        self._thread = None
        self.last_done = 0
        self.last_time = time.time()
        self.speed = 0.0
        self.retries = 0
        self.phase = phase

    def register(self, key, total):
        with self.lock:
            self.items[key] = {"total": max(int(total), 1), "done": 0, "status": "pending"}

    def update(self, key, done):
        with self.lock:
            if key in self.items:
                self.items[key]["done"] = done

    def mark_done(self, key):
        with self.lock:
            if key in self.items:
                item = self.items[key]
                item["done"] = item["total"]
                item["status"] = "done"

    def mark_failed(self, key):
        with self.lock:
            if key in self.items:
                self.items[key]["status"] = "failed"

    def bump_retry(self):
        with self.lock:
            self.retries += 1

    def _render(self):
        with self.lock:
            total = sum(i["total"] for i in self.items.values())
            done = sum(i["done"] for i in self.items.values())
            done_files = sum(1 for i in self.items.values() if i["status"] == "done")
            total_files = len(self.items)
            retries = self.retries

        now = time.time()
        dt = now - self.last_time
        if dt >= 0.3:
            instant = (done - self.last_done) / dt
            self.speed = 0.6 * self.speed + 0.4 * instant
            self.last_done = done
            self.last_time = now

        ratio = done / total if total else 0
        bar_width = 30
        filled = int(bar_width * ratio)
        bar = "█" * filled + "░" * (bar_width - filled)

        if self.speed < 2048:
            speed_str = "  -- B/s"
        else:
            speed_str = f"{human(self.speed)}/s"

        eta_str = ""
        if self.speed > 2048 and total > done:
            remain = (total - done) / self.speed
            eta_str = f" | ETA {int(remain // 60):02d}:{int(remain % 60):02d}"

        retry_str = f" | 重试 {retries}" if retries else ""
        phase_str = f" [{self.phase}]" if self.phase else ""

        line = (f"\r[{bar}] {ratio * 100:5.1f}% | "
                f"{human(done)}/{human(total)} | "
                f"{speed_str} | "
                f"{done_files}/{total_files} 文件{phase_str}{retry_str}{eta_str}")
        line += "\033[K"
        sys.stdout.write(line)
        sys.stdout.flush()

    def start(self):
        with self.lock:
            self.last_done = sum(i["done"] for i in self.items.values())
        self.last_time = time.time()

        def loop():
            while not self._stop.is_set():
                self._render()
                time.sleep(0.2)
            self._render()
            sys.stdout.write("\n")
            sys.stdout.flush()

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join()


def fetch_manifest(session, retries=DEFAULT_RETRIES):
    return fetch_json_safe(session, MANIFEST, retries=retries)


def choose_version(session, retries=DEFAULT_RETRIES):
    manifest = fetch_manifest(session, retries)
    versions = manifest["versions"]
    latest = manifest.get("latest", {})

    print(f"最新正式版: {latest.get('release', '?')}")
    print(f"最新快照:   {latest.get('snapshot', '?')}\n")
    print("筛选类型: [r] 正式版  [s] 快照  [o] 远古版本  [a] 全部")
    f = input("> ").strip().lower()

    type_map = {"r": ("release",), "s": ("snapshot",), "o": ("old_beta", "old_alpha")}
    if f in type_map:
        allowed = type_map[f]
        filtered = [v for v in versions if v["type"] in allowed]
    else:
        filtered = versions

    if not filtered:
        raise SystemExit("没有符合条件的版本")

    page_size, page = 15, 0
    total_pages = (len(filtered) + page_size - 1) // page_size

    while True:
        start = page * page_size
        chunk = filtered[start:start + page_size]
        print(f"\n--- 第 {page + 1}/{total_pages} 页（共 {len(filtered)} 个）---")
        for i, v in enumerate(chunk):
            idx = start + i
            print(f"  [{idx:4d}] {v['id']:<22} ({v['type']:<10}) {v.get('releaseTime', '')[:10]}")
        print("\n编号选择 | n 下一页 | p 上一页 | q 退出 | 或直接输入版本号")

        c = input("> ").strip()
        if c.lower() == "q":
            raise SystemExit("已取消")
        if c.lower() == "n" and page < total_pages - 1:
            page += 1
            continue
        if c.lower() == "p" and page > 0:
            page -= 1
            continue
        if c.isdigit():
            idx = int(c)
            if 0 <= idx < len(filtered):
                return filtered[idx]
            print("编号超出范围")
            continue
        hit = next((v for v in versions if v["id"] == c), None)
        if hit:
            return hit
        print("找不到该版本，请重试")


def find_version(session, version_id, retries=DEFAULT_RETRIES):
    manifest = fetch_manifest(session, retries)
    for v in manifest["versions"]:
        if v["id"] == version_id:
            return v
    raise SystemExit(f"版本不存在: {version_id}")


@dataclass
class Task:
    url: str
    dest: Path
    sha1: str = ""
    kind: str = ""
    size: int = 0


def os_name():
    return {"Windows": "windows", "Linux": "linux", "Darwin": "osx"}.get(
        platform.system(), platform.system().lower())


def check_sha1(path, expected):
    if not expected:
        return True
    if not path.exists():
        return False
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest() == expected


def download_one(url, dest, sha1=None, progress=None, key=None,
                 retries=DEFAULT_RETRIES):
    session = get_session()

    if sha1 and check_sha1(dest, sha1):
        if progress:
            progress.mark_done(key)
        return dest, "cached"

    dest.parent.mkdir(parents=True, exist_ok=True)
    attempts_pool = [resolve_url(url)]
    if url not in attempts_pool:
        attempts_pool.append(url)

    last_err = None
    last_class = "slow"
    retry_count = 0

    while retry_count <= retries:
        for attempt_url in attempts_pool:
            try:
                existing = dest.stat().st_size if dest.exists() else 0
                headers = {"Range": f"bytes={existing}-"} if existing else {}
                r = session.get(attempt_url, headers=headers, stream=True,
                                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))

                if r.status_code == 416:
                    if sha1 and not check_sha1(dest, sha1):
                        dest.unlink()
                        raise requests.exceptions.HTTPError("416 but sha1 mismatch")
                    if progress:
                        progress.mark_done(key)
                    return dest, "done"

                if r.status_code in (408, 429, 500, 502, 503, 504):
                    raise requests.exceptions.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()

                mode = "ab" if existing and r.status_code == 206 else "wb"
                received = existing if mode == "ab" else 0

                with open(dest, mode) as f:
                    try:
                        for chunk in r.iter_content(65536):
                            f.write(chunk)
                            received += len(chunk)
                            if progress:
                                progress.update(key, received)
                    except (requests.exceptions.ChunkedEncodingError,
                            http.client.IncompleteRead) as e:
                        last_err = e
                        last_class = "fast"
                        raise

                if sha1 and not check_sha1(dest, sha1):
                    dest.unlink()
                    if progress:
                        progress.update(key, 0)
                    raise requests.exceptions.HTTPError("sha1 mismatch")

                if progress:
                    progress.mark_done(key)
                return dest, "done"

            except Exception as e:
                last_err = e
                last_class = classify_error(e)
                continue

        retry_count += 1
        if retry_count <= retries:
            if progress:
                progress.bump_retry()
            if last_class == "fast":
                backoff = random.uniform(0.3, 1.5)
            else:
                backoff = min(2 ** retry_count, 30) + random.uniform(0, 1.0)
            time.sleep(backoff)

    raise last_err if last_err else RuntimeError(f"download failed: {url}")


def collect_tasks(session, entry, retries=DEFAULT_RETRIES):
    version_id = entry["id"]
    vjson = fetch_json_safe(session, entry["url"], retries=retries)

    # 持久化版本 JSON
    version_dir = ROOT / "versions" / version_id
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / f"{version_id}.json").write_text(
        json.dumps(vjson, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 版本隔离：建好每个版本独立的游戏数据目录
    for sub in ISOLATED_DIRS:
        (version_dir / sub).mkdir(parents=True, exist_ok=True)

    tasks = []

    client = vjson.get("downloads", {}).get("client")
    if client:
        tasks.append(Task(
            url=client["url"],
            dest=ROOT / "versions" / version_id / f"{version_id}.jar",
            sha1=client.get("sha1", ""),
            kind="client",
            size=client.get("size", 0),
        ))

    current_os = os_name()
    for lib in vjson.get("libraries", []):
        rules = lib.get("rules")
        allowed = True
        if rules:
            allowed = False
            for r in rules:
                if r.get("os", {}).get("name") == current_os:
                    allowed = (r.get("action") == "allow")
        if not allowed:
            continue

        dl = lib.get("downloads", {})
        art = dl.get("artifact")
        if art:
            tasks.append(Task(
                url=art["url"],
                dest=ROOT / "libraries" / art["path"],
                sha1=art.get("sha1", ""),
                kind="library",
                size=art.get("size", 0),
            ))

        natives = lib.get("natives", {})
        key = natives.get(current_os)
        if key:
            key = key.replace("${arch}", "32" if platform.architecture()[0] == "32bit" else "64")
            cls = dl.get("classifiers", {}).get(key)
            if cls:
                tasks.append(Task(
                    url=cls["url"],
                    dest=ROOT / "libraries" / cls["path"],
                    sha1=cls.get("sha1", ""),
                    kind="natives_jar",
                    size=cls.get("size", 0),
                ))

    ai = vjson.get("assetIndex")
    if ai:
        tasks.append(Task(
            url=ai["url"],
            dest=ROOT / "assets" / "indexes" / f"{ai['id']}.json",
            sha1=ai.get("sha1", ""),
            kind="asset_index",
            size=ai.get("size", 0),
        ))

    logfile = vjson.get("logging", {}).get("client", {}).get("file")
    if logfile:
        tasks.append(Task(
            url=logfile["url"],
            dest=ROOT / "assets" / "log_configs" / logfile["id"],
            sha1=logfile.get("sha1", ""),
            kind="logging",
            size=logfile.get("size", 0),
        ))

    return version_id, tasks


def expand_asset_index(index_path):
    data = json.loads(index_path.read_text(encoding="utf-8"))
    tasks = []
    for _, obj in data.get("objects", {}).items():
        h = obj["hash"]
        tasks.append(Task(
            url=f"{RESOURCES}/{h[:2]}/{h}",
            dest=ROOT / "assets" / "objects" / h[:2] / h,
            sha1=h,
            kind="asset",
            size=obj.get("size", 0),
        ))
    return tasks


def run_tasks(tasks, workers=DEFAULT_THREADS, retries=DEFAULT_RETRIES, proxy=None, phase=""):
    if not tasks:
        return [], None, 0

    progress = ProgressBar(phase=phase)
    for t in tasks:
        existing = t.dest.stat().st_size if t.dest.exists() else 0
        progress.register(str(t.dest), max(t.size, existing, 1))
        if existing > 0:
            progress.update(str(t.dest), existing)

    natives_jars = []
    asset_index_path = None
    failures = []

    progress.start()
    try:
        with ThreadPoolExecutor(
            max_workers=workers,
            initializer=worker_init,
            initargs=(proxy,),
        ) as pool:
            futures = {
                pool.submit(download_one, t.url, t.dest, t.sha1,
                            progress, str(t.dest), retries): t
                for t in tasks
            }
            for fut in as_completed(futures):
                task = futures[fut]
                try:
                    dest, _ = fut.result()
                    if task.kind == "natives_jar":
                        natives_jars.append(dest)
                    if task.kind == "asset_index":
                        asset_index_path = dest
                except Exception as e:
                    failures.append((task.dest, str(e)))
                    progress.mark_failed(str(task.dest))
    finally:
        progress.stop()

    if failures:
        print(f"\n❌ {len(failures)} 个文件失败：")
        for dest, err in failures[:10]:
            print(f"  {dest}: {err}")
        if len(failures) > 10:
            print(f"  ... 还有 {len(failures) - 10} 个")

    return natives_jars, asset_index_path, len(failures)


def extract_natives(jars, target):
    target.mkdir(parents=True, exist_ok=True)
    for jar in jars:
        if not jar.exists():
            continue
        try:
            with zipfile.ZipFile(jar) as z:
                for name in z.namelist():
                    if name.startswith("META-INF/") or name.endswith("/"):
                        continue
                    z.extract(name, target)
        except zipfile.BadZipFile:
            print(f"  跳过损坏的 natives jar: {jar}")


def main():
    args = parse_args()

    threads = None
    if args.threads is not None:
        threads = validate_threads(args.threads)
    elif args.version:
        threads = DEFAULT_THREADS

    retries = validate_retries(args.retries)
    session = build_session(args.proxy)

    if args.version:
        entry = find_version(session, args.version, retries=retries)
    else:
        entry = choose_version(session, retries=retries)

    if threads is None:
        threads = ask_threads(DEFAULT_THREADS)

    asset_threads = min(threads * ASSET_THREAD_MULTIPLIER, ASSET_THREAD_CAP)

    if args.proxy:
        proxy_info = args.proxy
    else:
        env_proxy = next(
            (os.environ.get(k) for k in
             ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
             if os.environ.get(k)), None)
        proxy_info = f"未使用（已忽略环境变量 {env_proxy}）" if env_proxy else "直连 / TUN 系统代理"

    print(f"\n选中版本: {entry['id']} ({entry['type']})")
    print(f"核心线程: {threads}")
    print(f"资源线程: {asset_threads}（小文件多，并发弥补 RTT）")
    print(f"代理:     {proxy_info}")
    print(f"重试上限: {retries}")
    print(f"版本隔离: versions/{entry['id']}/ 下建 {len(ISOLATED_DIRS)} 个游戏目录\n")

    version_id, tasks = collect_tasks(session, entry, retries=retries)
    print(f"核心文件: {len(tasks)} 个\n")

    natives_jars, asset_index_path, failed = run_tasks(
        tasks, workers=threads, retries=retries, proxy=args.proxy, phase="核心")

    if asset_index_path and asset_index_path.exists():
        asset_tasks = expand_asset_index(asset_index_path)
        print(f"\n资源文件: {len(asset_tasks)} 个，{asset_threads} 线程\n")
        _, _, asset_failed = run_tasks(
            asset_tasks, workers=asset_threads, retries=retries,
            proxy=args.proxy, phase="资源")
        failed += asset_failed

    if natives_jars:
        natives_dir = ROOT / "versions" / version_id / "natives"
        print(f"\n解压 {len(natives_jars)} 个 natives JAR ...")
        extract_natives(natives_jars, natives_dir)
        print(f"natives -> {natives_dir}")

    if failed:
        print(f"\n⚠ 有 {failed} 个文件失败，重新运行会跳过已成功的文件")
        sys.exit(1)
    print(f"\n✅ {version_id} 完整下载完成，游戏目录: versions/{version_id}/")


if __name__ == "__main__":
    main()
