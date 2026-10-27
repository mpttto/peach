#!/usr/bin/env python3
"""单架构镜像的构建、验收与多架构索引发布。

每个架构在自己的构建任务里构建一次，同时产出两份内容一致的产物：`type=docker`
归档加载进本机 Docker 供冒烟，`type=oci` 布局留在磁盘上供发布；归档加载成功后删除，
只在磁盘上保留布局。发布时用
`docker buildx imagetools create` 把两个单架构 manifest 合成索引并推送，不重新构建。
每个架构都是单架构镜像，加载与运行不依赖 containerd image store。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "longmeidao/peach"
PLATFORMS = ("linux/amd64", "linux/arm64")
ARCHS = {"linux/amd64": "amd64", "linux/arm64": "arm64"}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
TOKEN = re.compile(r"[A-Za-z0-9_.-]+")
SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
COMMIT = re.compile(r"[0-9a-f]{40}")


class DockerImageError(RuntimeError):
    """布局、回执或发布条件不满足。"""


def checksum(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _blob(layout: Path, digest: str) -> Path:
    if not DIGEST.fullmatch(str(digest)):
        raise DockerImageError("镜像 digest 无效")
    return layout / "blobs" / "sha256" / digest.split(":", 1)[1]


def _platform(entry: dict) -> str:
    platform = entry.get("platform") or {}
    return f"{platform.get('os')}/{platform.get('architecture')}"


def _layout_entry(layout: Path, platform: str) -> dict:
    """单架构布局的 `index.json` 只应有一个该平台的 manifest 条目。"""
    index = json.loads((layout / "index.json").read_text(encoding="utf-8"))
    matches = [entry for entry in index.get("manifests") or [] if _platform(entry) == platform]
    if len(matches) != 1:
        raise DockerImageError(f"OCI 布局必须只包含一个 {platform} manifest")
    return matches[0]


def _verify_layout(layout: Path, platform: str, manifest: str, config: str) -> None:
    """核对布局里的 manifest、config 与每个图层都在磁盘上且内容与 digest 相符。"""
    entry = _layout_entry(layout, platform)
    if entry.get("digest") != manifest:
        raise DockerImageError(f"{platform} 布局的 manifest 与回执不符")
    path = _blob(layout, manifest)
    if not path.is_file() or checksum(path.read_bytes()) != manifest:
        raise DockerImageError(f"{platform} 布局的 manifest 内容与 digest 不符")
    document = json.loads(path.read_text(encoding="utf-8"))
    if (document.get("config") or {}).get("digest") != config:
        raise DockerImageError(f"{platform} 布局的 config 与回执不符")
    if not _blob(layout, config).is_file():
        raise DockerImageError(f"{platform} 布局缺少 config")
    for layer in document.get("layers") or []:
        blob = _blob(layout, layer.get("digest"))
        if not blob.is_file() or blob.stat().st_size != layer.get("size"):
            raise DockerImageError(f"{platform} 布局缺少图层 {layer.get('digest')}")


def build(destination: Path, *, platform: str, version: str, commit: str,
          cache_from: list[str] | None = None, cache_to: list[str] | None = None,
          build_args: list[str] | None = None, run=subprocess.run) -> dict:
    """构建一个架构：加载进本机 Docker，并把同一份内容写成可发布的 OCI 布局。"""
    if platform not in PLATFORMS:
        raise DockerImageError("构建平台无效")
    destination = destination.resolve()
    if destination == ROOT or ROOT.is_relative_to(destination):
        raise DockerImageError("镜像输出目录不能包含仓库根")
    if not SEMVER.fullmatch(version):
        raise DockerImageError("镜像版本无效")
    if not COMMIT.fullmatch(commit):
        raise DockerImageError("镜像提交无效")
    arch = ARCHS[platform]
    layout = destination / arch
    archive = destination / f"{arch}.tar"
    metadata = destination / f"{arch}-build.json"
    if layout.exists() and any(layout.iterdir()):
        raise DockerImageError("镜像输出目录必须为空")
    destination.mkdir(parents=True, exist_ok=True)
    reference = f"peach-build:{arch}"
    command = [
        "docker", "buildx", "build",
        "--platform", platform,
        "-t", reference,
        "--output", f"type=docker,dest={archive}",
        "--output", f"type=oci,dest={layout},tar=false",
        "--metadata-file", str(metadata),
        *[f"--cache-from={value}" for value in (cache_from or [])],
        *[f"--cache-to={value}" for value in (cache_to or [])],
        *[f"--build-arg={value}" for value in (build_args or [])],
        "--provenance=false", "--sbom=false",
        "--label", f"org.opencontainers.image.version={version}",
        "--label", f"org.opencontainers.image.revision={commit}",
        str(ROOT),
    ]
    run(command, check=True)
    manifest = json.loads(metadata.read_text(encoding="utf-8")).get("containerimage.digest")
    if not DIGEST.fullmatch(str(manifest)):
        raise DockerImageError("构建元数据没有给出有效的 manifest digest")
    document = json.loads(_blob(layout, manifest).read_text(encoding="utf-8"))
    config = (document.get("config") or {}).get("digest")
    _verify_layout(layout, platform, manifest, config)
    run(["docker", "load", "-i", str(archive)], check=True)
    # 镜像 ID 在 containerd 与 classic 存储驱动上分别取 manifest 与 config digest，
    # 不能用来核对布局；按与存储无关的构建标签核对加载的是本次构建。
    labels = json.loads(run(["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", reference],
                            capture_output=True, text=True, check=True).stdout)
    if (not isinstance(labels, dict)
            or labels.get("org.opencontainers.image.version") != version
            or labels.get("org.opencontainers.image.revision") != commit):
        raise DockerImageError("加载的镜像不是本次构建的版本与提交")
    # 冒烟用加载后的镜像，归档与布局内容重复，加载成功即可回收，供标签发布只留布局。
    archive.unlink()
    receipt ={"format": 2, "image": IMAGE, "version": version, "commit": commit,
               "platform": platform, "manifest": manifest, "config": config, "layout": arch}
    (destination / f"{arch}.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def _arch_fields(receipt: dict) -> None:
    if (receipt.get("format") != 2 or receipt.get("image") != IMAGE
            or receipt.get("platform") not in PLATFORMS
            or not SEMVER.fullmatch(str(receipt.get("version")))
            or not COMMIT.fullmatch(str(receipt.get("commit")))
            or not DIGEST.fullmatch(str(receipt.get("manifest")))
            or not DIGEST.fullmatch(str(receipt.get("config")))
            or not TOKEN.fullmatch(str(receipt.get("layout")))):
        raise DockerImageError("单架构镜像回执的版本、提交、平台或 digest 无效")


def _receipt_fields(receipt: dict) -> None:
    platforms = receipt.get("platforms")
    if (receipt.get("format") != 2 or receipt.get("image") != IMAGE
            or not SEMVER.fullmatch(str(receipt.get("version")))
            or not COMMIT.fullmatch(str(receipt.get("commit")))
            or not isinstance(platforms, dict) or set(platforms) != set(PLATFORMS)):
        raise DockerImageError("镜像回执的版本、提交或平台无效")
    for platform, entry in platforms.items():
        if (not isinstance(entry, dict) or not DIGEST.fullmatch(str(entry.get("manifest")))
                or not DIGEST.fullmatch(str(entry.get("config")))
                or not TOKEN.fullmatch(str(entry.get("layout")))):
            raise DockerImageError(f"{platform} 的 manifest、config 或布局无效")


def record(receipts: list[Path], destination: Path, *, version: str, commit: str) -> dict:
    """把两个单架构回执合成发布回执；版本与提交必须一致。"""
    platforms = {}
    for path in receipts:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DockerImageError(f"无法读取单架构回执：{error}") from error
        _arch_fields(data)
        if data["version"] != version or data["commit"] != commit:
            raise DockerImageError("单架构回执与发布提交或版本不符")
        if data["platform"] in platforms:
            raise DockerImageError(f"重复的 {data['platform']} 回执")
        platforms[data["platform"]] = {"manifest": data["manifest"], "config": data["config"],
                                       "layout": data["layout"]}
    if set(platforms) != set(PLATFORMS):
        raise DockerImageError("必须同时登记 linux/amd64 与 linux/arm64")
    receipt = {"format": 2, "image": IMAGE, "version": version, "commit": commit,
               "platforms": platforms}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def verify(receipt_path: Path, *, layouts: Path) -> dict:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DockerImageError(f"无法读取镜像回执：{error}") from error
    _receipt_fields(receipt)
    root = layouts.resolve()
    for platform, entry in receipt["platforms"].items():
        layout = (root / entry["layout"]).resolve()
        if not layout.is_relative_to(root) or layout.is_symlink():
            raise DockerImageError("镜像布局越出给定目录或包含软链接")
        _verify_layout(layout, platform, entry["manifest"], entry["config"])
    return receipt


def publish(receipt_path: Path, tags: list[str], *, layouts: Path,
            repository: str = IMAGE, run=subprocess.run) -> dict:
    """把两个单架构 manifest 合成索引，同一索引推送到每个标签并回读核对。"""
    if not tags or any(not TOKEN.fullmatch(tag) for tag in tags):
        raise DockerImageError("发布标签无效")
    receipt = verify(receipt_path, layouts=layouts)
    root = layouts.resolve()
    sources = [f"oci-layout://{(root / receipt['platforms'][platform]['layout']).resolve()}"
               f"@{receipt['platforms'][platform]['manifest']}" for platform in PLATFORMS]
    references = [f"{repository}:{tag}" for tag in tags]
    metadata = receipt_path.parent / "publish.json"
    command = ["docker", "buildx", "imagetools", "create", "--metadata-file", str(metadata)]
    for reference in references:
        command += ["-t", reference]
    run([*command, *sources], check=True)
    index = (json.loads(metadata.read_text(encoding="utf-8")).get("containerimage.descriptor") or {}).get("digest")
    if not DIGEST.fullmatch(str(index)):
        raise DockerImageError("无法读取发布后的索引 digest")
    expected = {platform: receipt["platforms"][platform]["manifest"] for platform in PLATFORMS}
    for reference in references:
        raw = run(["docker", "buildx", "imagetools", "inspect", "--raw", reference],
                  capture_output=True, text=True, check=True).stdout
        if checksum(raw.strip().encode()) != index:
            raise DockerImageError(f"registry 中的索引与发布结果不符：{reference}")
        manifest = json.loads(raw)
        platforms = {_platform(entry): entry.get("digest") for entry in manifest.get("manifests") or []}
        if {key: platforms.get(key) for key in PLATFORMS} != expected:
            raise DockerImageError(f"registry 中的平台 manifest 与回执不符：{reference}")
    return {"published": references, "index": index}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    building = sub.add_parser("build", help="构建单架构镜像、加载供冒烟并写回执")
    building.add_argument("--destination", type=Path, required=True)
    building.add_argument("--platform", required=True, choices=PLATFORMS)
    building.add_argument("--version", required=True)
    building.add_argument("--commit", required=True)
    building.add_argument("--cache-from", action="append", default=[])
    building.add_argument("--cache-to", action="append", default=[])
    building.add_argument("--build-arg", action="append", default=[])
    recording = sub.add_parser("record", help="把单架构回执合成发布回执")
    recording.add_argument("--receipt", type=Path, action="append", required=True)
    recording.add_argument("--destination", type=Path, required=True)
    recording.add_argument("--version", required=True)
    recording.add_argument("--commit", required=True)
    verifying = sub.add_parser("verify", help="核对回执与磁盘上的 OCI 布局")
    verifying.add_argument("--receipt", type=Path, required=True)
    verifying.add_argument("--layouts", type=Path, required=True)
    publishing = sub.add_parser("publish", help="合成多架构索引并推送")
    publishing.add_argument("--receipt", type=Path, required=True)
    publishing.add_argument("--layouts", type=Path, required=True)
    publishing.add_argument("--tag", action="append", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build(args.destination, platform=args.platform, version=args.version,
                           commit=args.commit, cache_from=args.cache_from,
                           cache_to=args.cache_to, build_args=args.build_arg)
        elif args.command == "record":
            result = record(args.receipt, args.destination, version=args.version, commit=args.commit)
        elif args.command == "verify":
            result = verify(args.receipt, layouts=args.layouts)
        else:
            result = publish(args.receipt, args.tag, layouts=args.layouts)
    except (DockerImageError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"镜像处理失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
