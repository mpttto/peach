"""GitHub CI 通过官方 REST 接口晋级同一提交的已验收产物，不调用 CLI。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from peach.user_agent import USER_AGENT

REQUIRED_ARTIFACTS = {"desktop-candidate", "macos-candidate-arm64", "macos-candidate-x86_64", "docker-image"}
REQUIRED_JOBS = {"Windows x64 独立测试包", "仅下载制品验收",
    "Linux amd64 镜像构建与验收", "Linux arm64 镜像构建与验收", "Linux 多架构镜像登记",
    "macOS arm64 独立测试包", "macOS x86_64 独立测试包",
    "macOS arm64 仅下载制品验收", "macOS x86_64 仅下载制品验收"}


class ArtifactRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        if urlsplit(url).scheme != "https":
            raise ValueError("GitHub 产物重定向必须使用 HTTPS")
        redirected = super().redirect_request(request, file, code, message, headers, url)
        if redirected is not None and urlsplit(url).hostname != "api.github.com":
            redirected.remove_header("Authorization")
        return redirected


def request(repo: str, endpoint: str):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if os.environ.get("GITHUB_ACTIONS") == "true" and token:
        headers["Authorization"] = "Bearer " + token
    return build_opener(ArtifactRedirect()).open(Request(f"https://api.github.com/repos/{repo}/{endpoint}", headers=headers), timeout=60)


def api(repo: str, endpoint: str):
    with request(repo, endpoint) as response:
        return json.load(response)


def select(repo: str, sha: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("GitHub 晋级需要明确仓库与完整提交")
    runs = api(repo, f"actions/workflows/release.yml/runs?head_sha={sha}&event=push&per_page=100")["workflow_runs"]
    matching = [row for row in runs if row["head_sha"] == sha and row["head_branch"] == "master" and row["event"] == "push"]
    if not matching:
        raise ValueError("未取得同一提交的主线发布演练")
    run = max(matching, key=lambda row: (row["id"], row.get("run_attempt", 1)))
    if run["status"] != "completed" or run["conclusion"] != "success":
        raise ValueError("主线发布演练尚未成功")
    jobs = api(repo, f"actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
    passed = {row["name"] for row in jobs if row["conclusion"] == "success"}
    if not REQUIRED_JOBS <= passed:
        raise ValueError("演练缺少必需的构建或独立制品验收")
    artifacts = api(repo, f"actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
    selected = {row["name"]: row for row in artifacts if row["name"] in REQUIRED_ARTIFACTS and not row["expired"]}
    if set(selected) != REQUIRED_ARTIFACTS or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", str(row.get("digest", ""))) for row in selected.values()):
        raise ValueError("候选产物已过期、缺失或缺少摘要；重新执行演练")
    return {"run_id": run["id"], "commit": sha, "artifacts": selected}


def extract(package: Path, destination: Path) -> None:
    with zipfile.ZipFile(package) as archive:
        for info in archive.infolist():
            parts = PurePosixPath(info.filename).parts
            if (not parts or ".." in parts or "\\" in info.filename or ":" in info.filename
                    or PurePosixPath(info.filename).is_absolute() or (info.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("候选 ZIP 包含无效路径")
            target = destination.joinpath(*parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("候选 ZIP 路径越出目标")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                with archive.open(info) as source, target.open("rb") as current:
                    if hashlib.file_digest(source, "sha256").digest() != hashlib.file_digest(current, "sha256").digest():
                        raise ValueError("候选文件同名但内容不同")
                continue
            with archive.open(info) as source, target.open("xb") as stream:
                shutil.copyfileobj(source, stream)


def download(repo: str, sha: str, group: str, destination: Path) -> dict:
    receipt = select(repo, sha)
    names = {"desktop": {"desktop-candidate"}, "macos": {"macos-candidate-arm64", "macos-candidate-x86_64"},
             "docker": {"docker-image"}}[group]
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="peach-candidate-") as directory:
        for name in sorted(names):
            row = receipt["artifacts"][name]
            package = Path(directory) / (name + ".zip")
            with request(repo, f"actions/artifacts/{row['id']}/zip") as response, package.open("wb") as stream:
                shutil.copyfileobj(response, stream)
            with package.open("rb") as stream:
                actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != row["digest"]:
                raise ValueError("下载产物与演练的 SHA-256 不一致")
            extract(package, destination)
    return {"promoted": True, "run_id": receipt["run_id"], "commit": sha, "group": group}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=("desktop", "macos", "docker"))
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    result = download(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"], args.group, args.destination)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
