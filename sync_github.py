"""把本地工作区同步到 GitHub（走 Git Data API，绕开 git 协议）。

首次运行会点亮空仓库；之后每次运行都基于远端 main 的现有树做增量覆盖，
只上传内容变了的文件，历史保持 一条线性。
"""
import base64
import hashlib
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OWNER, REPO = "ixxgal", "music-desk"


def token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    raise SystemExit("拿不到 GitHub 凭据")


TOK = token()


def api(method: str, path: str, body=None, timeout=90, allow=(200,)):
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {TOK}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "MusicDesk-Sync")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in allow:
            return {"_status": e.code}
        raise SystemExit(f"{method} {path} → {e.code}\n{e.read().decode(errors='replace')[:400]}")


def local_blob_sha(raw: bytes) -> str:
    """git 的 blob sha = sha1("blob <len>\\0" + content)"""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(raw))
    h.update(raw)
    return h.hexdigest()


def main():
    files = [f for f in subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, cwd=ROOT
    ).stdout.decode("utf-8").split("\0") if f]

    # 远端现状
    ref = api("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/main", allow=(200, 404))
    head_sha = ref.get("object", {}).get("sha") if "_status" not in ref else None
    remote_blobs: dict[str, str] = {}
    if head_sha:
        head = api("GET", f"/repos/{OWNER}/{REPO}/git/commits/{head_sha}")
        base_tree = head["tree"]["sha"]
        tree = api("GET", f"/repos/{OWNER}/{REPO}/git/trees/{base_tree}?recursive=1")
        remote_blobs = {e["path"]: e["sha"] for e in tree.get("tree", []) if e["type"] == "blob"}
    else:
        base_tree = None

    # 逐个比对，只传变了的
    entries, uploaded = [], 0
    for rel in files:
        posix = rel.replace("\\", "/")
        raw = (ROOT / rel).read_bytes()
        sha = local_blob_sha(raw)
        if remote_blobs.get(posix) == sha:
            entries.append({"path": posix, "mode": "100644", "type": "blob", "sha": sha})
            continue
        blob = api("POST", f"/repos/{OWNER}/{REPO}/git/blobs",
                   {"content": base64.b64encode(raw).decode(), "encoding": "base64"})
        entries.append({"path": posix, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        uploaded += 1
        print(f"  ↑ {posix}  {len(raw)}B")

    print(f"共 {len(files)} 个文件，本次上传 {uploaded} 个")

    if uploaded == 0 and head_sha:
        print("远端已是最新，无需提交")
        return

    new_tree = api("POST", f"/repos/{OWNER}/{REPO}/git/trees",
                   {"tree": entries, "base_tree": base_tree} if base_tree
                   else {"tree": entries})

    msg = subprocess.run(["git", "log", "-1", "--pretty=%B"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", cwd=ROOT).stdout.strip() or "update"
    payload = {"message": msg, "tree": new_tree["sha"]}
    if head_sha:
        payload["parents"] = [head_sha]
    commit = api("POST", f"/repos/{OWNER}/{REPO}/git/commits", payload)
    print("commit:", commit["sha"])

    if head_sha:
        api("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/main",
            {"sha": commit["sha"], "force": True})
    else:
        api("POST", f"/repos/{OWNER}/{REPO}/git/refs",
            {"ref": "refs/heads/main", "sha": commit["sha"]})
    print("main 已更新 →", commit["html_url"])


if __name__ == "__main__":
    main()