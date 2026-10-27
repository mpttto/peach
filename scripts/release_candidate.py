"""核对受保护演练的产物清单；标签发布同一份已验收字节。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import re
import sys
import tempfile

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"
from . import ci_r2_artifacts as artifacts
from .release_forge import GitLabForge, REQUIRED_TEST_JOBS, REHEARSAL_JOBS
from .release_r2 import require_capacity

GROUPS = ("windows-release", "macos-release", "docker-release")
RECEIPT = Path("build/ci-artifacts/release-candidate.json")


def forge() -> GitLabForge:
    def command(*args):
        return subprocess.check_output(args, text=True, encoding="utf-8", stderr=subprocess.PIPE)
    return GitLabForge(os.environ["CI_SERVER_HOST"], command)


def verified_jobs(client, repo: str, build: dict) -> dict[str, int]:
    selected = client.jobs(repo, int(build["pipeline"]))
    required = (REQUIRED_TEST_JOBS | REHEARSAL_JOBS) - {"seal-release"}
    bad = [name for name in sorted(required) if name not in selected
           or selected[name]["status"] != "success" or selected[name].get("allow_failure")
           or selected[name].get("commit", {}).get("id") != build["commit"]]
    if bad:
        raise ValueError("演练缺少同一提交的成功验证：" + "、".join(bad))
    return {name: selected[name]["id"] for name in sorted(required)}


def listing(root: Path) -> list[dict]:
    environment = artifacts.rclone_environment()
    with tempfile.TemporaryDirectory(prefix="peach-release-capacity-") as directory:
        config = Path(directory) / "rclone.conf"
        config.touch()
        return json.loads(subprocess.check_output([artifacts.rclone_executable(root), "lsjson",
            f"peach:{environment['PEACH_R2_BUCKET']}", "--recursive", "--files-only", "--config", str(config)],
            env=environment, text=True, encoding="utf-8"))


def manifests(root: Path, build: dict) -> dict:
    result = {}
    for group in GROUPS:
        manifest = artifacts.remote_json(root, artifacts.checkpoint(build, group))
        if manifest is None:
            raise ValueError("缺少完整上传的演练产物：" + group)
        artifacts.validate_manifest(manifest, group, build)
        result[group] = manifest
    return result


def publication_items(groups: dict) -> list[dict]:
    result = {}
    for group in ("windows-release", "macos-release"):
        for item in groups[group]["files"]:
            path = PurePosixPath(item["path"])
            if path.suffix not in {".zip", ".exe", ".dmg"}:
                continue
            row = {"name": path.name, "size": item["size"], "sha256": item["sha256"]}
            if path.name in result and result[path.name] != row:
                raise ValueError("发行文件同名但内容不一致")
            result[path.name] = row
    return list(result.values())


def check_capacity(root: Path, build: dict | None = None, groups: dict | None = None) -> dict:
    build = build or artifacts.identity()
    groups = groups or manifests(root, build)
    rows = listing(root)
    limit = int(os.environ.get("PEACH_R2_MAX_BYTES", "8000000000"))
    projected = require_capacity(rows, publication_items(groups), build["tag"], limit)
    return {"current_bytes": sum(row["Size"] for row in rows if not row.get("IsDir")),
            "projected_bytes": projected, "limit_bytes": limit}


def preflight(root: Path) -> dict:
    """构建前取得真实桶占用；精确发行峰值在清单齐备后、安装冒烟前核对。"""
    artifacts.identity()
    rows = listing(root)
    limit = int(os.environ.get("PEACH_R2_MAX_BYTES", "8000000000"))
    current = sum(row["Size"] for row in rows if not row.get("IsDir"))
    if limit <= 0 or current >= limit:
        raise ValueError(f"发布桶当前占用 {current} 字节，预算 {limit}；先审阅暂存清理方案")
    scopes = {}
    for row in rows:
        parts = row["Path"].split("/")
        if len(parts) >= 4 and parts[0] == "_ci":
            scope = "/".join(parts[:4])
            scopes[scope] = scopes.get(scope, 0) + row["Size"]
    return {"current_bytes": current, "limit_bytes": limit, "staging_bytes": scopes,
            "publication_peak": "构建清单齐备后精确核对；不按未知新版体积伪造剩余容量"}


def seal(root: Path) -> dict:
    build = artifacts.identity()
    if os.environ.get("CI_COMMIT_BRANCH") != "master" or os.environ.get("CI_COMMIT_TAG"):
        raise ValueError("只有受保护主线的发布演练可以登记候选")
    client = forge()
    jobs = verified_jobs(client, os.environ["CI_PROJECT_PATH"], build)
    groups = manifests(root, build)
    capacity = check_capacity(root, build, groups)
    path = root / "build/ci-artifacts/docker-image.json"
    (root / "build/ci-artifacts").mkdir(parents=True, exist_ok=True)
    (root / "build/ci-artifacts/docker-release.json").write_text(json.dumps(groups["docker-release"]), encoding="utf-8")
    artifacts.transfer(root, "docker-release", "pull")
    docker = json.loads(path.read_text(encoding="utf-8"))
    validate_docker(docker, build)
    candidate = {"format": 1, "identity": build, "jobs": jobs, "groups": groups, "docker": docker}
    artifacts.remote_json(root, artifacts.prefix(build) + "/release-candidate.json", candidate)
    return {"sealed": True, "identity": build, "capacity": capacity}


def validate_docker(docker: dict, build: dict) -> None:
    platforms = docker.get("platforms")
    if (docker.get("format") != 2 or docker.get("commit") != build["commit"]
            or docker.get("pipeline") != build["pipeline"] or docker.get("version") != build["tag"][1:]
            or not isinstance(platforms, dict) or set(platforms) != {"linux/amd64", "linux/arm64"}
            or not all(isinstance(entry, dict)
                       and re.fullmatch(r"sha256:[0-9a-f]{64}", str(entry.get("manifest")))
                       and re.fullmatch(r"sha256:[0-9a-f]{64}", str(entry.get("config")))
                       and re.fullmatch(r"[A-Za-z0-9_.-]+", str(entry.get("layout")))
                       for entry in platforms.values())):
        raise ValueError("Docker 候选镜像不属于同一提交与演练")


def validate(root: Path) -> dict:
    target = artifacts.identity()
    if os.environ.get("CI_COMMIT_TAG") != target["tag"]:
        raise ValueError("候选晋级只接受受保护版本标签")
    client, repo = forge(), os.environ["CI_PROJECT_PATH"]
    runs = client.test_runs(repo, target["commit"])
    if not runs or runs[0]["status"] != "completed" or runs[0]["conclusion"] != "success":
        raise ValueError("同一提交的主线发布演练尚未成功")
    source = {**target, "pipeline": str(runs[0]["id"])}
    client.require_rehearsal(repo, int(source["pipeline"]))
    candidate = artifacts.remote_json(root, artifacts.prefix(source) + "/release-candidate.json")
    if not isinstance(candidate, dict) or candidate.get("format") != 1 or candidate.get("identity") != source:
        raise ValueError("候选清单不属于当前项目、提交、版本与演练")
    if candidate.get("jobs") != verified_jobs(client, repo, source) or set(candidate.get("groups", {})) != set(GROUPS):
        raise ValueError("候选的验证记录与演练最新成功任务不一致")
    for group, manifest in candidate["groups"].items():
        artifacts.validate_manifest(manifest, group, source)
    validate_docker(candidate.get("docker", {}), source)
    receipt = {"target": target, "candidate": candidate}
    path = root / RECEIPT
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != receipt:
        raise ValueError("本标签已选择另一份候选，保留原回执并停止")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def pull(root: Path) -> dict:
    receipt = validate(root)
    candidate = receipt["candidate"]
    for group in GROUPS:
        manifest = candidate["groups"][group]
        path = root / "build/ci-artifacts" / (group + ".json")
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        artifacts.transfer(root, group, "pull", source=candidate["identity"])
    return {"promoted": True, "source_pipeline": candidate["identity"]["pipeline"], "commit": candidate["identity"]["commit"]}


def cleanup(root: Path) -> dict:
    receipt = json.loads((root / RECEIPT).read_text(encoding="utf-8"))
    target = artifacts.identity()
    if receipt.get("target") != target:
        raise ValueError("回收回执不属于当前标签流水线")
    client, repo = forge(), os.environ["CI_PROJECT_PATH"]
    runs = client.test_runs(repo, target["commit"])
    if not runs or runs[0]["status"] != "completed" or runs[0]["conclusion"] != "success":
        raise ValueError("回收前未取得成功演练")
    source = {**target, "pipeline": str(runs[0]["id"])}
    if (receipt["candidate"].get("identity") != source
            or receipt["candidate"].get("jobs") != verified_jobs(client, repo, source)):
        raise ValueError("回收回执与成功演练不一致")
    client.require_rehearsal(repo, int(source["pipeline"]))
    return artifacts.cleanup(root, source=source)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "check-capacity", "seal", "validate", "pull", "cleanup"))
    args = parser.parse_args()
    functions = {"preflight": preflight, "check-capacity": check_capacity,
                 "seal": seal, "validate": validate, "pull": pull, "cleanup": cleanup}
    result = functions[args.mode](Path.cwd().resolve())
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
