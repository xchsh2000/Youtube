import base64
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path


OWNER = "xchsh2000"
REPO = "Youtube"
BRANCH = "main"
API_ROOT = "https://api.github.com"


ROOT = Path(__file__).resolve().parent
GIT_DIR = ROOT / ".gitdata"
if not GIT_DIR.exists():
    GIT_DIR = ROOT / ".git"


def git_bytes(*args):
    cmd = ["git", f"--git-dir={GIT_DIR}", f"--work-tree={ROOT}", *args]
    return subprocess.check_output(cmd, cwd=ROOT)


def git_text(*args):
    return git_bytes(*args).decode("utf-8", errors="replace").strip()


def request_json(method, path, token, payload=None, allow_404=False):
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        f"{API_ROOT}{path}",
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "youtube-local-uploader",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            if not data:
                return None
            return json.loads(data.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if allow_404 and exc.code in (404, 409):
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {path} failed: HTTP {exc.code}\n{detail}") from exc


def tracked_files():
    raw = git_bytes("ls-tree", "-r", "-z", "--name-only", "HEAD")
    files = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        path = item.decode("utf-8", errors="replace")
        if path.startswith(".gitdata/") or path.startswith(".ssh_github/"):
            continue
        files.append(path)
    return files


def upload_with_contents_api(token, files, message):
    print("检测到空仓库，改用 Contents API 创建初始文件。")
    for rel_path in files:
        full_path = ROOT / rel_path
        api_path = urllib.parse.quote(rel_path.replace("\\", "/"), safe="/")
        current = request_json(
            "GET",
            f"/repos/{OWNER}/{REPO}/contents/{api_path}?ref={BRANCH}",
            token,
            allow_404=True,
        )
        payload = {
            "message": message,
            "content": base64.b64encode(full_path.read_bytes()).decode("ascii"),
            "branch": BRANCH,
        }
        if current and current.get("sha"):
            payload["sha"] = current["sha"]
        request_json(
            "PUT",
            f"/repos/{OWNER}/{REPO}/contents/{api_path}",
            token,
            payload,
        )
        print(f"已上传：{rel_path}")


def main():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("缺少 GITHUB_TOKEN。请通过 上传到GitHub_API.bat 输入 GitHub token。")

    short_sha = git_text("rev-parse", "--short", "HEAD")
    message = git_text("log", "-1", "--pretty=%s") or "Upload project"
    files = tracked_files()
    if not files:
        raise SystemExit("没有找到要上传的已提交文件。")

    print(f"准备上传 {len(files)} 个文件到 {OWNER}/{REPO}:{BRANCH}")

    ref = request_json(
        "GET",
        f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}",
        token,
        allow_404=True,
    )
    if ref is None:
        upload_with_contents_api(token, files, f"{message}\n\nInitial upload from local commit: {short_sha}")
        print(f"上传完成：https://github.com/{OWNER}/{REPO}")
        return

    parents = []
    base_tree = None
    parent_sha = ref["object"]["sha"]
    parents = [parent_sha]
    parent_commit = request_json(
        "GET",
        f"/repos/{OWNER}/{REPO}/git/commits/{parent_sha}",
        token,
    )
    base_tree = parent_commit["tree"]["sha"]

    tree_entries = []
    for rel_path in files:
        full_path = ROOT / rel_path
        content = full_path.read_bytes()
        blob = request_json(
            "POST",
            f"/repos/{OWNER}/{REPO}/git/blobs",
            token,
            {
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            },
        )
        mode = "100755" if rel_path.lower().endswith((".bat", ".ps1")) else "100644"
        tree_entries.append(
            {
                "path": rel_path.replace("\\", "/"),
                "mode": mode,
                "type": "blob",
                "sha": blob["sha"],
            }
        )
        print(f"已准备：{rel_path}")

    tree_payload = {"tree": tree_entries}
    tree_payload["base_tree"] = base_tree

    tree = request_json(
        "POST",
        f"/repos/{OWNER}/{REPO}/git/trees",
        token,
        tree_payload,
    )
    commit = request_json(
        "POST",
        f"/repos/{OWNER}/{REPO}/git/commits",
        token,
        {
            "message": f"{message}\n\nLocal commit: {short_sha}",
            "tree": tree["sha"],
            "parents": parents,
        },
    )

    request_json(
        "PATCH",
        f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}",
        token,
        {"sha": commit["sha"], "force": False},
    )

    print(f"上传完成：https://github.com/{OWNER}/{REPO}")
    print(f"GitHub commit: {commit['sha']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"上传失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
