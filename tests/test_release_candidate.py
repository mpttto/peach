import copy
import hashlib
import json
import os
import io
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from urllib.request import Request
import zipfile
from unittest import mock

from scripts import ci_r2_artifacts as artifacts
from scripts import docker_image
from scripts import release_candidate as candidate
from scripts import github_release_candidate as github
from scripts.release_forge import REQUIRED_TEST_JOBS, REHEARSAL_JOBS


class ReleaseCandidateTests(unittest.TestCase):
    def setUp(self):
        self.build = {"project": "123", "pipeline": "7", "commit": "a" * 40, "tag": "v1.2.3"}
        self.environment = {"CI_PROJECT_ID": "123", "CI_PIPELINE_ID": "8", "CI_COMMIT_SHA": "a" * 40,
            "CI_COMMIT_TAG": "v1.2.3", "CI_COMMIT_REF_PROTECTED": "true", "CI_SERVER_HOST": "forge.example",
            "CI_PROJECT_PATH": "owner/repo"}
        self.names = sorted((REQUIRED_TEST_JOBS | REHEARSAL_JOBS) - {"seal-release"})
        self.jobs = {name: {"id": index + 10, "status": "success", "commit": {"id": "a" * 40}}
                     for index, name in enumerate(self.names)}
        self.docker = {"format": 2, "commit": "a" * 40, "pipeline": "7", "version": "1.2.3",
            "platforms": {platform: {"manifest": "sha256:" + digest * 64, "config": "sha256:" + config * 64,
                                     "layout": arch}
                          for platform, digest, config, arch in (
                              ("linux/amd64", "d", "1", "amd64"), ("linux/arm64", "e", "2", "arm64"))}}
        self.paths = {"windows-release": "release/Peach-1.2.3-windows-x64.zip",
            "macos-release": "dist/macos.noindex/arm64/Peach-1.2.3-macos-arm64.dmg",
            "docker-release": "build/ci-artifacts/docker-image.json"}
        self.groups = {group: {"format": 1, "identity": self.build, "producer_job": "99", "group": group,
            "files": [{"path": path, "size": 4, "sha256": hashlib.sha256(b"file").hexdigest()}]}
            for group, path in self.paths.items()}
        self.sealed = {"format": 1, "identity": self.build, "jobs": {name: row["id"] for name, row in self.jobs.items()},
            "groups": self.groups, "docker": self.docker}
        self.forge = mock.Mock()
        self.forge.test_runs.return_value = [{"id": 7, "status": "completed", "conclusion": "success"}]
        self.forge.jobs.return_value = self.jobs

    def test_tag_validation_binds_successful_jobs_and_same_commit_candidate(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, self.environment, clear=True), \
                mock.patch.object(candidate, "forge", return_value=self.forge), \
                mock.patch.object(artifacts, "remote_json", return_value=self.sealed):
            root = Path(directory).resolve()
            receipt = candidate.validate(root)
            self.assertEqual(receipt["candidate"]["identity"]["pipeline"], "7")
            self.assertEqual(receipt["target"]["pipeline"], "8")
            self.assertTrue((root / candidate.RECEIPT).is_file())
            self.forge.require_rehearsal.assert_called_once_with("owner/repo", 7)

    def test_changed_commit_failed_checks_or_replaced_mirror_are_rejected(self):
        mutations = (lambda data: data["identity"].update(commit="c" * 40),
                     lambda data: data["jobs"].update({self.names[0]: 999}),
                     lambda data: data["docker"].update(platforms={
                         "linux/amd64": data["docker"]["platforms"]["linux/amd64"]}))
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory, \
                    mock.patch.dict(os.environ, self.environment, clear=True), \
                    mock.patch.object(candidate, "forge", return_value=self.forge):
                data = copy.deepcopy(self.sealed)
                mutate(data)
                with mock.patch.object(artifacts, "remote_json", return_value=data), self.assertRaises(ValueError):
                    candidate.validate(Path(directory).resolve())
        self.jobs[self.names[0]]["status"] = "failed"
        with self.assertRaises(ValueError):
            candidate.verified_jobs(self.forge, "owner/repo", self.build)

    def test_missing_candidate_is_not_a_successful_promotion(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, self.environment, clear=True), \
                mock.patch.object(candidate, "forge", return_value=self.forge), \
                mock.patch.object(artifacts, "remote_json", return_value=None), self.assertRaises(ValueError):
            candidate.validate(Path(directory).resolve())

    def test_pull_keeps_source_pipeline_identity_and_group_file_checksums(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, self.environment, clear=True), \
                mock.patch.object(candidate, "forge", return_value=self.forge), \
                mock.patch.object(artifacts, "remote_json", return_value=self.sealed), \
                mock.patch.object(artifacts, "transfer", return_value={"files": 1}) as transfer:
            root = Path(directory).resolve()
            result = candidate.pull(root)
            self.assertTrue(result["promoted"])
            self.assertEqual(transfer.call_count, 3)
            self.assertTrue(all(call.kwargs["source"] == self.build for call in transfer.call_args_list))
            manifest = json.loads((root / "build/ci-artifacts/windows-release.json").read_text())
            self.assertEqual(manifest["files"], self.groups["windows-release"]["files"])

    def test_peak_capacity_includes_staging_and_new_public_files(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(candidate, "listing", return_value=[{"Path": "_ci/current", "Size": 7999999995}]), \
                self.assertRaisesRegex(ValueError, "超过上限"):
            candidate.check_capacity(Path(directory), self.build, self.groups)

    def test_unprotected_tags_and_foreign_source_identity_cannot_read_artifacts(self):
        with mock.patch.dict(os.environ, {**self.environment, "CI_COMMIT_REF_PROTECTED": "false"}, clear=True), \
                self.assertRaises(ValueError):
            candidate.validate(Path.cwd())
        with mock.patch.dict(os.environ, self.environment, clear=True), self.assertRaises(ValueError):
            artifacts.transfer(Path.cwd(), "windows-release", "pull", source={**self.build, "project": "other"})

    def test_restore_distinguishes_absence_from_failed_reads(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, self.environment, clear=True), \
                mock.patch.object(artifacts, "remote_json", return_value=None) as read:
            root = Path(directory).resolve()
            self.assertFalse(artifacts.restore(root, "windows-release")["restored"])
            read.side_effect = subprocess.CalledProcessError(1, "rclone")
            with self.assertRaises(subprocess.CalledProcessError):
                artifacts.restore(root, "windows-release")

    def test_checkpoint_restore_downloads_without_publishing_new_build(self):
        build = {**self.build, "pipeline": "8"}
        manifest = {**self.groups["windows-release"], "identity": build}
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, self.environment, clear=True), \
                mock.patch.object(artifacts, "remote_json", return_value=manifest), \
                mock.patch.object(artifacts, "transfer", return_value={"files": 1}) as transfer:
            root = Path(directory).resolve()
            self.assertTrue(artifacts.restore(root, "windows-release")["restored"])
            transfer.assert_called_once_with(root, "windows-release", "pull")

    def test_sealed_rehearsal_and_tag_transfer_keep_identical_bytes(self):
        metadata, objects = {}, {}
        for group, path in self.paths.items():
            data = json.dumps(self.docker).encode() if group == "docker-release" else b"verified " + group.encode()
            item = self.groups[group]["files"][0]
            item.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
            metadata[artifacts.checkpoint(self.build, group)] = self.groups[group]
            remote = f"peach:peach-releases/{artifacts.prefix(self.build, group, '99')}/{path}"
            objects[remote] = data
        def remote_json(_root, key, payload=None):
            if payload is not None:
                metadata[key] = copy.deepcopy(payload)
            return copy.deepcopy(metadata.get(key))
        def transfer(command, **_kwargs):
            self.assertEqual(command[1], "copy")
            source, destination = command[2:4]
            names = Path(command[command.index("--files-from-raw") + 1]).read_text().splitlines()
            for name in names:
                path = Path(destination) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(objects[source + "/" + name])
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(candidate, "forge", return_value=self.forge), \
                mock.patch.object(candidate, "listing", return_value=[]), \
                mock.patch.object(artifacts, "remote_json", side_effect=remote_json), \
                mock.patch.object(artifacts, "rclone_environment", return_value={"PEACH_R2_BUCKET": "peach-releases"}), \
                mock.patch.object(artifacts, "rclone_executable", return_value="rclone"), \
                mock.patch.object(artifacts.subprocess, "run", side_effect=transfer):
            root = Path(directory).resolve()
            source = root / "rehearsal"
            source.mkdir()
            rehearsal = {**self.environment, "CI_PIPELINE_ID": "7", "CI_COMMIT_BRANCH": "master",
                         "CI_COMMIT_TAG": "", "PEACH_RELEASE_REHEARSAL": "1"}
            with mock.patch.dict(os.environ, rehearsal, clear=True), mock.patch.object(artifacts, "rehearsal_tag", return_value="v1.2.3"):
                self.assertTrue(candidate.seal(source)["sealed"])
            target = root / "tag"
            target.mkdir()
            with mock.patch.dict(os.environ, self.environment, clear=True):
                self.assertTrue(candidate.pull(target)["promoted"])
            self.assertEqual((target / self.paths["windows-release"]).read_bytes(), b"verified windows-release")
            self.assertEqual(json.loads((target / self.paths["docker-release"]).read_text()), self.docker)

@unittest.skipUnless(shutil.which("pwsh"), "需要 PowerShell 7 执行 Windows 发布入口")
class DockerPublicationTests(unittest.TestCase):
    def test_failed_publish_keeps_the_verified_layouts_and_success_removes_them(self):
        entry = Path(__file__).resolve().parents[1] / "scripts/ci_windows.ps1"
        platforms = {platform: {"manifest": "sha256:" + digest * 64, "config": "sha256:" + config * 64,
                                "layout": arch}
                     for platform, digest, config, arch in (
                         ("linux/amd64", "d", "1", "amd64"), ("linux/arm64", "e", "2", "arm64"))}
        docker = {"format": 2, "commit": "a" * 40, "pipeline": "7", "version": "1.2.3",
                  "platforms": platforms}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "src/peach").mkdir(parents=True)
            (root / "src/peach/__init__.py").write_text('__version__ = "1.2.3"')
            (root / "scripts").mkdir()
            (root / "scripts/docker_image.py").write_text(
                'import os, pathlib, sys\n'
                'command = sys.argv[1]\n'
                'if command == "verify":\n'
                '    raise SystemExit(0 if pathlib.Path(sys.argv[-1]).is_dir() else 2)\n'
                'if command == "publish":\n'
                '    raise SystemExit(int(os.environ.get("TEST_FAIL_PUSH", "0")))\n'
                'raise SystemExit(3)\n', encoding="utf-8")
            (root / "build/ci-artifacts").mkdir(parents=True)
            receipt = {"target": {"pipeline": "8", "commit": "a" * 40},
                       "candidate": {"identity": {"pipeline": "7"}, "docker": docker}}
            (root / "build/ci-artifacts/release-candidate.json").write_text(json.dumps(receipt))
            driver = root / "driver.ps1"
            driver.write_text('''function docker {
    param([Parameter(Position=0, ValueFromRemainingArguments=$true)][string[]]$DockerArgs,
          [Parameter(ValueFromPipeline=$true)][string]$LoginInput)
    Add-Content -LiteralPath calls.log -Value ($DockerArgs -join ' ')
    $global:LASTEXITCODE = 0
    if ($DockerArgs[0] -eq 'context') { return 'fixture-endpoint' }
}
try { & $env:TEST_CI_ENTRY docker-push } catch { Write-Output $_; exit 3 }
''', encoding="utf-8")
            builds = root / "builds"
            layouts = builds / ".peach-docker-layouts/123" / ("a" * 40)
            environment = {**os.environ, "TEST_CI_ENTRY": str(entry), "CI_BUILDS_DIR": str(builds),
                "CI_PROJECT_ID": "123", "CI_PIPELINE_ID": "8", "CI_COMMIT_SHA": "a" * 40,
                "CI_COMMIT_TAG": "v1.2.3", "PEACH_RELEASE_REHEARSAL": "",
                "DOCKERHUB_USERNAME": "fixture", "DOCKERHUB_TOKEN": "fixture"}
            for fail in (True, False):
                with self.subTest(failed=fail):
                    log = root / "calls.log"
                    log.unlink(missing_ok=True)
                    for arch in ("amd64", "arm64"):
                        (layouts / arch).mkdir(parents=True, exist_ok=True)
                        (layouts / f"{arch}.json").write_text("{}")
                    result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-File", str(driver)],
                        cwd=root, env={**environment, "TEST_FAIL_PUSH": str(int(fail))}, text=True, encoding="utf-8", capture_output=True)
                    calls = log.read_text(encoding="utf-8-sig")
                    self.assertEqual(result.returncode == 0, not fail, (result.stdout or "") + (result.stderr or ""))
                    self.assertEqual(layouts.exists(), fail)
                    self.assertIn("login", calls)
                    self.assertIn("logout", calls)


class DockerImageTests(unittest.TestCase):
    amd64_manifest = "sha256:" + "1" * 64
    amd64_config = "sha256:" + "2" * 64
    arm64_manifest = "sha256:" + "3" * 64
    arm64_config = "sha256:" + "4" * 64
    index = "sha256:" + "5" * 64

    def layout(self, root: Path, arch: str, platform: str) -> tuple[Path, str, str]:
        """写一个最小 OCI 布局：index、manifest、config 与一个图层，digest 按内容计算。"""
        folder = root / arch
        blobs = folder / "blobs/sha256"
        blobs.mkdir(parents=True, exist_ok=True)
        layer = ("layer " + arch).encode()
        layer_digest = "sha256:" + hashlib.sha256(layer).hexdigest()
        config = json.dumps({"architecture": arch}).encode()
        config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
        document = {"mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                               "digest": config_digest, "size": len(config)},
                    "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar",
                                "digest": layer_digest, "size": len(layer)}]}
        manifest = json.dumps(document).encode()
        manifest_digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        (blobs / config_digest.split(":", 1)[1]).write_bytes(config)
        (blobs / manifest_digest.split(":", 1)[1]).write_bytes(manifest)
        (blobs / layer_digest.split(":", 1)[1]).write_bytes(layer)
        (folder / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [
            {"mediaType": document["mediaType"], "digest": manifest_digest, "size": len(manifest),
             "platform": {"os": "linux", "architecture": arch}}]}))
        return folder, manifest_digest, config_digest

    def build(self, root: Path, platform: str = "linux/amd64") -> tuple[dict, list[list[str]]]:
        destination = root / "build/docker"
        arch = platform.split("/")[1]
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            if command[:3] == ["docker", "buildx", "build"]:
                _, manifest, _ = self.layout(destination, arch, platform)
                (destination / f"{arch}-build.json").write_text(json.dumps({"containerimage.digest": manifest}))
                (destination / f"{arch}.tar").write_bytes(b"docker archive")
            elif command[:2] == ["docker", "load"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            elif command[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(command, 0, json.dumps({
                    "org.opencontainers.image.version": "1.2.3",
                    "org.opencontainers.image.revision": "a" * 40}), "")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(docker_image, "ROOT", root):
            receipt = docker_image.build(destination, platform=platform, version="1.2.3",
                                         commit="a" * 40, run=run)
        return receipt, commands

    def test_build_loads_the_single_architecture_and_writes_a_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            receipt, commands = self.build(root)
            self.assertEqual(receipt["platform"], "linux/amd64")
            self.assertTrue(docker_image.DIGEST.fullmatch(receipt["manifest"]))
            self.assertTrue(docker_image.DIGEST.fullmatch(receipt["config"]))
            self.assertEqual(receipt["layout"], "amd64")
            build = commands[0]
            self.assertIn("type=oci", " ".join(build))
            self.assertIn("type=docker", " ".join(build))
            self.assertNotIn("--load", build, "加载由回执核对后的 docker load 完成")
            self.assertIn("--platform", build)
            self.assertEqual(build[build.index("--platform") + 1], "linux/amd64")
            self.assertIn(["--provenance=false", "--sbom=false"], [build[i:i + 2] for i in range(len(build) - 1)])
            self.assertIn("docker", commands[1][0])

    def test_verify_rejects_a_changed_receipt_or_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            receipt, _ = self.build(root)
            destination = root / "build/docker"
            path = destination / "receipt.json"
            combined = {"format": 2, "image": "longmeidao/peach", "version": "1.2.3", "commit": "a" * 40,
                        "platforms": {"linux/amd64": {"manifest": receipt["manifest"],
                                                      "config": receipt["config"], "layout": "amd64"}}}
            path.write_text(json.dumps(combined))
            with self.assertRaises(docker_image.DockerImageError):
                docker_image.verify(path, layouts=destination)
            _, arm_manifest, arm_config = self.layout(destination, "arm64", "linux/arm64")
            combined["platforms"]["linux/arm64"] = {"manifest": arm_manifest,
                                                    "config": arm_config, "layout": "arm64"}
            path.write_text(json.dumps(combined))
            self.assertEqual(docker_image.verify(path, layouts=destination)["platforms"], combined["platforms"])
            (destination / "amd64/blobs/sha256" / receipt["config"].split(":", 1)[1]).unlink()
            with self.assertRaisesRegex(docker_image.DockerImageError, "config"):
                docker_image.verify(path, layouts=destination)

    def test_publish_merges_both_layouts_and_checks_the_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            layouts = root / "build/docker"
            _, amd_manifest, amd_config = self.layout(layouts, "amd64", "linux/amd64")
            _, arm_manifest, arm_config = self.layout(layouts, "arm64", "linux/arm64")
            receipt = {"format": 2, "image": "longmeidao/peach", "version": "1.2.3", "commit": "a" * 40,
                       "platforms": {"linux/amd64": {"manifest": amd_manifest,
                                                     "config": amd_config, "layout": "amd64"},
                                     "linux/arm64": {"manifest": arm_manifest,
                                                     "config": arm_config, "layout": "arm64"}}}
            path = layouts / "receipt.json"
            path.write_text(json.dumps(receipt))
            calls = []
            raw = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                              "manifests": [
                                  {"platform": {"os": "linux", "architecture": "amd64"}, "digest": amd_manifest},
                                  {"platform": {"os": "linux", "architecture": "arm64"}, "digest": arm_manifest}]})
            index = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()

            def run(command, **kwargs):
                calls.append(command)
                if command[:3] == ["docker", "buildx", "imagetools"] and command[3] == "create":
                    metadata = Path(command[command.index("--metadata-file") + 1])
                    metadata.write_text(json.dumps({"containerimage.descriptor": {
                        "mediaType": "application/vnd.oci.image.index.v1+json", "digest": index, "size": 1}}))
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[:3] == ["docker", "buildx", "imagetools"]:
                    return subprocess.CompletedProcess(command, 0, raw, "")
                return subprocess.CompletedProcess(command, 0, "", "")

            result = docker_image.publish(path, ["1.2.3", "edge"], layouts=layouts, run=run)
            self.assertEqual(result["index"], index)
            self.assertEqual(result["published"], ["longmeidao/peach:1.2.3", "longmeidao/peach:edge"])
            create = calls[0]
            self.assertTrue(any(source.startswith("oci-layout://") and source.endswith("@" + amd_manifest)
                                for source in create))
            self.assertTrue(any(source.startswith("oci-layout://") and source.endswith("@" + arm_manifest)
                                for source in create))
            self.assertIn("longmeidao/peach:edge", create)

    def test_a_single_architecture_or_foreign_commit_cannot_be_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            amd64, _ = self.build(root)
            path = root / "build/docker/amd64.json"
            with self.assertRaisesRegex(docker_image.DockerImageError, "linux/arm64"):
                docker_image.record([path], root / "receipt.json", version="1.2.3", commit="a" * 40)
            arm64, _ = self.build(root, "linux/arm64")
            with self.assertRaisesRegex(docker_image.DockerImageError, "提交或版本"):
                docker_image.record([path, root / "build/docker/arm64.json"], root / "receipt.json",
                                    version="1.2.3", commit="b" * 40)
            combined = docker_image.record([path, root / "build/docker/arm64.json"], root / "receipt.json",
                                           version="1.2.3", commit="a" * 40)
            self.assertEqual(set(combined["platforms"]), {"linux/amd64", "linux/arm64"})
            self.assertEqual(combined["platforms"]["linux/arm64"]["manifest"], arm64["manifest"])

    def test_a_layout_with_the_wrong_platform_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = Path(directory)
            self.layout(layout.parent, layout.name + "-amd", "linux/amd64")
            with self.assertRaisesRegex(docker_image.DockerImageError, "linux/arm64"):
                docker_image._layout_entry(layout.parent / (layout.name + "-amd"), "linux/arm64")


class GitHubCandidateTests(unittest.TestCase):
    def payload(self, name="program.exe"):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as package:
            package.writestr(name, b"verified bytes")
        return stream.getvalue()

    def test_download_checks_server_digest_before_extracting(self):
        payload = self.payload()
        record = {"run_id": 7, "artifacts": {"desktop-candidate": {"id": 9,
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest()}}}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(github, "select", return_value=record), \
                mock.patch.object(github, "request", side_effect=lambda *_: io.BytesIO(payload)):
            root = Path(directory)
            self.assertTrue(github.download("owner/repo", "a" * 40, "desktop", root)["promoted"])
            self.assertEqual((root / "program.exe").read_bytes(), b"verified bytes")
            record["artifacts"]["desktop-candidate"]["digest"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                github.download("owner/repo", "a" * 40, "desktop", root / "second")
            self.assertFalse((root / "second/program.exe").exists())

    def test_verified_zip_cannot_escape_or_replace_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "artifact.zip"
            path.write_bytes(self.payload("../outside"))
            with self.assertRaises(ValueError):
                github.extract(path, root / "target")
            self.assertFalse((root / "outside").exists())
            path.write_bytes(self.payload())
            (root / "program.exe").write_bytes(b"keep original")
            with self.assertRaises(ValueError):
                github.extract(path, root)
            self.assertEqual((root / "program.exe").read_bytes(), b"keep original")

    def test_cross_host_redirect_drops_ci_authorization(self):
        original = Request("https://api.github.com/repos/owner/repo/actions/artifacts/9/zip",
                           headers={"Authorization": "Bearer test-value"})
        redirected = github.ArtifactRedirect().redirect_request(original, None, 302, "Found", {}, "https://artifacts.example/package")
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaises(ValueError):
            github.ArtifactRedirect().redirect_request(original, None, 302, "Found", {}, "http://artifacts.example/package")

    def test_expired_artifacts_and_skipped_smoke_are_not_successful_candidates(self):
        run = {"id": 7, "head_sha": "a" * 40, "head_branch": "master", "event": "push", "status": "completed", "conclusion": "success"}
        jobs = [{"name": name, "conclusion": "success"} for name in github.REQUIRED_JOBS]
        artifacts = [{"name": name, "id": index, "expired": False, "digest": "sha256:" + "b" * 64}
                     for index, name in enumerate(sorted(github.REQUIRED_ARTIFACTS))]
        def api(_repo, endpoint):
            return {"workflow_runs": [run]} if "workflows/" in endpoint else {"jobs": jobs} if "/jobs?" in endpoint else {"artifacts": artifacts}
        with mock.patch.object(github, "api", side_effect=api):
            self.assertEqual(github.select("owner/repo", "a" * 40)["run_id"], 7)
            artifacts[0]["expired"] = True
            with self.assertRaises(ValueError):
                github.select("owner/repo", "a" * 40)
            artifacts[0]["expired"] = False
            jobs[0]["conclusion"] = "skipped"
            with self.assertRaises(ValueError):
                github.select("owner/repo", "a" * 40)

    def test_docker_group_promotes_the_combined_receipt_without_a_tagged_image(self):
        payload = self.payload("receipt.json")
        record = {"run_id": 7, "artifacts": {"docker-image": {"id": 9,
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest()}}}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(github, "select", return_value=record), \
                mock.patch.object(github, "request", side_effect=lambda *_: io.BytesIO(payload)):
            root = Path(directory)
            self.assertTrue(github.download("owner/repo", "a" * 40, "docker", root)["promoted"])
            self.assertEqual((root / "receipt.json").read_bytes(), b"verified bytes")
