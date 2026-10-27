# ADR-0129：多架构容器镜像按架构构建、按 manifest 合并

- 状态：已采纳
- 日期：2026-10-27
- 关系：修订 [ADR-0105](0105-linux-headless-container.md) 的 amd64 初始范围与 [ADR-0127](0127-verified-release-artifact-promotion.md) 的镜像身份与发布核对

## 背景

ADR-0105 把容器镜像的初始目标限定为 amd64，ARM 需实际验收后再纳入。Apple Silicon 的 Docker Desktop 与主流 ARM NAS 直接运行 `linux/arm64`；只提供 amd64 镜像时，这些机器只能模拟运行，启动、转码与播放都多一层开销。

ADR-0127 用加载后的 image ID 记录演练镜像。这个身份只在单次构建、单台机器上成立：GitHub 与 GitLab 各自重建会得到不同 image ID，而标签发布又要求核对同一个镜像，于是镜像只能留在构建机上，另一个 forge 无法复用。

## 决策

### 平台

镜像同时提供 `linux/amd64` 与 `linux/arm64`。darwin 不是容器平台；macOS 的 Docker Desktop 在 Apple Silicon 上运行 linux/arm64，在 Intel Mac 上运行 linux/amd64，由 Docker 自动选择。不发布 darwin 容器镜像。

### 按架构构建

每个架构在自己的构建任务里构建一次，同时产出两份内容一致的产物：`type=docker` 归档加载进本机 Docker 供冒烟，`type=oci` 布局留在磁盘上供发布。归档加载成功后即删除，磁盘上只保留布局。Dockerfile 不按架构分支；基础镜像与 uv 镜像都提供两个架构。

- GitHub 用原生 runner：amd64 在 `ubuntu-latest`，arm64 在 `ubuntu-24.04-arm`，都不经过模拟。
- GitLab 只有一台 Windows Docker Desktop runner，两个架构都在本机构建；arm64 由 Docker Desktop 自带的 QEMU 运行。

构建不使用多平台归档，也不需要 containerd image store：每个架构都是单架构镜像，`docker load` 与 `docker run` 在默认存储驱动下即可工作。

### 身份与发布

单架构回执记录该架构的 manifest digest、config digest 与布局目录；发布回执把两个平台合并，并带上版本与提交。标签只在发布时出现：`docker buildx imagetools create` 从磁盘上的两个 OCI 布局合成一个索引，同一索引推送到版本号与 `edge`，再从 registry 读回逐个平台核对 manifest digest。不重新构建，不创建 `candidate` 或架构后缀标签。

两个 CI 都可以对同一组标签独立发布；后发布者覆盖为它自己构建并验收的索引。不同 forge 的 digest 不同不构成冲突。

### 演练与发布之间

GitHub 把两个单架构布局与回执作为工作流产物传输，发布前重新核对布局里每个 blob 的 digest。GitLab 把布局按项目与提交留在 runner 的 `CI_BUILDS_DIR` 下，标签流水线按回执核对本机的同一份布局，不重新构建，也不依赖工作区。

## 后果

- 构建时长与磁盘占用约为单平台的两倍，但两个架构在 GitHub 上并行、都不经模拟。
- GitLab 的 arm64 构建与验收受 QEMU 速度限制，这是该 runner 唯一的模拟点。
- 发布身份是「每个平台的 manifest digest + 版本 + 提交」。ADR-0127 的其余晋级规则不变：标签只晋级已验收字节，不重新构建。

## 验收边界

容器冒烟覆盖两个平台的应用行为、软件转码回退、人脸模型离线加载与运行时架构断言。ARM NAS 与 Apple Silicon 的实机播放、ARM SoC 或 AMD/Intel GPU 的硬件转码不属于容器验收，仍需在部署机器上验证。Docker Hub 的多架构索引以发布后从 registry 读回的结果为准。
