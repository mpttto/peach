# Docker 部署

Peach 的 Docker 镜像用于单人 VPS 与 NAS，通过浏览器管理，镜像仓库为 [longmeidao/peach](https://hub.docker.com/r/longmeidao/peach)。每次发布推送版本号标签，并把 `edge` 更新为同一份多架构索引；版本号标签对应发布提交。镜像同时提供 `linux/amd64` 与 `linux/arm64`：Apple Silicon 的 Docker Desktop 自动选择 arm64，Intel Mac、x86_64 VPS 与常见 x86 NAS 使用 amd64，ARM NAS 使用 arm64。2026-10-15 在 WSL 的独立 Docker Engine 中，用正式锁定依赖通过空数据卷初始化、非 root、鉴权、配置保存、合成视频扫描、HTTP Range、配置重载与重启持久化完成 amd64 验收；两个架构的同一套容器冒烟由 CI 分别执行，ARM 实机仍需在部署机器验证，见 [ADR-0129](adr/0129-multiarch-images-build-per-architecture.md)。

Docker 不使用桌面托盘。在网页配置中添加媒体目录时，只需填写容器内实际目录，例如 `/media`；Peach 自动保存所需的路径配置。Windows 安装包、macOS 源码运行和 Docker 的入口分别见 [README](../README.md#下载与使用)。

## 数据与媒体

NAS 的 SMB 共享也可作为[SMB 媒体来源](SMB.md)直接连接，按账号权限读写，无需挂入媒体目录；Peach 数据卷仍单独保留。

`/data` 保存配置、SQLite 数据库、凭据和图片缓存，必须使用数据卷保存，重建容器时保留该卷。媒体目录单独挂到 `/media`；默认只读，扫描和播放不需要媒体写权限。整理、移动与删除需要使用者明确提供可写挂载。

CloudDrive2 与 Peach 分别运行。NAS 已有 CloudDrive2 时直接复用；全新机器按 [CloudDrive2 官方 Docker 指南](https://www.clouddrive2.com/docker.html) 安装并绑定网盘。让 Peach 容器也能读取 CloudDrive2 后续挂上的网盘目录，需要 Linux 宿主机支持 FUSE，并将 CloudDrive2 的挂载目录配置为可共享挂载变化；Compose 中的 `rslave` 用于让这些变化进入 Peach 容器。此机制不适用于 Docker Desktop，详见 [Docker 挂载传播说明](https://docs.docker.com/engine/storage/bind-mounts/#configure-bind-propagation)。Peach 容器不需要 FUSE 或特权模式。目录权限必须允许容器用户 UID 1000 读取。

需要云下载或推送发现时，Peach 配置中的 CloudDrive2 API 地址使用同一容器网络的服务名或可达的宿主机地址；容器里的 `127.0.0.1` 只指向 Peach 自己。

仓库根的 `compose.yaml` 使用 `PEACH_MEDIA_ROOT` 指定宿主机已有媒体根；容器中固定为 `/media`。先完成 CloudDrive2 挂载，再启动 Peach。命名卷 `peach-data` 保存 `/data`，不要执行删除该卷的操作。需要绑定现有数据目录时，先做完整备份并确保 UID 1000 可写；不要让另一台 Peach 同时写这份 Peach 数据库。

## 首次运行与访问

### 飞牛、其他 NAS 与局域网访问

浏览器在另一台电脑上时，访问地址使用 NAS 的局域网 IP。Compose 默认的 `127.0.0.1` 只允许宿主机访问；容器显示运行成功不代表局域网端口已开放。

在 `compose.yaml` 同目录创建 `.env`，将下面的示例 IP 和目录换成 NAS 的实际值。`PEACH_PORT` 是浏览器使用的宿主机端口，可自行选择：

```dotenv
PEACH_BIND=192.0.2.10
PEACH_PORT=8900
PEACH_MEDIA_ROOT=/srv/videos
```

`192.0.2.10` 是文档示例地址，不能直接用于部署。NAS 管理界面支持 Compose 环境变量时，也可在那里填写这三个值。应用配置并重新创建容器后，打开 `http://<NAS 局域网 IP>:<PEACH_PORT>`。不要在另一台电脑上用 `127.0.0.1` 访问 NAS。

登录口令由 Peach 随机生成，没有通用初始密码。在 NAS 的容器管理界面打开 Peach 容器终端，运行 `peach token` 查看；通过宿主机 Compose 终端时运行 `docker compose exec peach peach token`。登录后设置访问密码。

### 初始化与管理

使用 Compose 启动后，Peach 会为全新数据卷创建配置、数据库和随机登录口令。已有配置保持原值；如果已有数据库但缺少配置文件，服务拒绝启动，需要恢复配置文件。初始化日志只显示口令文件位置。

在容器终端运行 `peach token` 取得登录口令，在浏览器登录后设置访问密码。配置页要求登录，并且连接必须来自容器本机或私有网络；携带代理转发头的管理请求会被拒绝。通过配置页保存后，服务自动读取配置；勾选扫描时会启动资料处理。

容器内 HTTP 端口固定为 8900，访问端口在 Compose 设置。默认只发布到宿主机 `127.0.0.1`，远程访问使用私有 VPN 或 SSH 隧道。需要局域网访问时，通过 `PEACH_BIND` 绑定宿主机的局域网地址，并设置访问密码。公网托管边界见 [安全说明](../SECURITY.md)。Docker 镜像不提供多人账号或租户隔离。

## 配置文件与路径

配置文件位于数据卷内的 `/data/config.toml`。在浏览器配置页的「媒体目录」中添加目录：填写 `/media` 或 `/media/115` 等容器内目录，选择媒体来源和读取策略，再保存。已有目录改了位置时，修改这一行的媒体目录；数据库路径的对应关系由 Peach 管理。

手动编辑配置文件前先备份，编辑后重启容器。更新镜像保留这个文件，因此已有文件的注释也保持原样。查看配置说明不需要重新初始化数据卷。

容器已更新并重新启动后，在 `compose.yaml` 所在目录运行以下命令，可查看新版注释与当前配置值；命令只显示内容，不改写 `/data/config.toml`：

```sh
docker compose exec peach python -c "from peach import settings_file as s; print(s.render(s.active()), end='')"
```

默认模板只包含初始值，不包含已有部署的媒体目录和自定义设置。用默认模板整份替换会同时替换这些值，可能使已有影片无法读取。只更新说明时可以替换注释，保留配置值；需要替换整个文件时，先备份并逐项核对差异。

以下说明供手动编辑配置或在多台 Peach 之间对应已有媒体目录时参考。网页保存时会处理 `[media.locations]` 和 `[media.mounts]`：前者保存数据库中的路径前缀，后者保存实际读取文件的目录。Peach 数据库在所有系统上都使用 Windows 格式保存影片路径；Linux 无需创建相同的盘符或目录。

全新配置不预设媒体目录。盘符不决定来源，115 与 PikPak 均可选择任意已挂载目录；容器内填写实际路径即可。文档中的盘符只是数据库路径示例。

以下三个路径各有用途。假设宿主机媒体目录为 `/srv/videos`，Compose 将它挂到容器 `/media`：

| 位置 | 示例 | 用途 |
| --- | --- | --- |
| 宿主机目录 | `/srv/videos` | 填在 Compose 的 `PEACH_MEDIA_ROOT`，目录必须已存在 |
| 容器内目录 | `/media` | 填在配置页的媒体目录或 `[media.mounts]`，Peach 实际读取这里 |
| 数据库中的目录前缀 | `R:\media` | `[media.locations]` 的示例值，对应数据库影片路径的开头；通常由 Peach 管理 |

手动配置全新本地馆藏时，可使用以下对应关系。网页自动生成的前缀可能不同，使用时无需改成示例值。将两个表中的条目合入已有配置，不要重复添加同名表：

```toml
[media.locations]
local = 'R:\media'

[media.mounts]
local = '/media'
```

扫描 `/media/a.mp4` 后，Peach 在数据库的影片 `path` 字段中保存 `R:\media\a.mp4`；播放时再把 `R:\media` 替换为 `/media`，读取容器中的 `/media/a.mp4`。数据库中的这段 Windows 路径是字符串，不是 Linux 上实际存在的文件名。

已有影片时，`[media.locations]` 必须继续匹配数据库中的路径前缀。把它改成 `/media` 不会同步修改已有影片记录，反而会使路径无法对应。只搬动实际媒体目录时，保留 `[media.locations]`，调整 `[media.mounts]` 和 Compose 的目录挂载。一个来源有多个目录时，两张表均使用数组，数量和顺序一一对应。

需要复用另一台 Peach 的数据库时，在配置文件的 `[media.locations]` 中填写与该数据库影片路径匹配的目录前缀，在 `[media.mounts]` 中填写这台容器能读取的目录，例如 `/media/115`；已有部署保留自身的对应关系。Compose 必须已挂入这些目录。CloudDrive2 是媒体来源之一，本地目录也可直接挂入 Peach。

容器中的服务固定监听 `0.0.0.0:8900`，宿主机访问地址和端口使用 Compose 的 `PEACH_BIND`、`PEACH_PORT`；`[server]` 的 `host`、`port` 用于直接运行 `peach serve`。独立 Docker 部署保持 `[replication] enabled = false`，无需配置桌面托盘或 SMB 复制。

## 硬件转码（AMD / Intel VAAPI）

Linux 首次重编码时会探测实际可用的硬件编码器，整片 MP4 与 HLS 分片共享探测结果。AMD / Intel 使用 VAAPI；存在 NVIDIA 设备时探测 NVENC。探测失败的后端在当前服务进程内不再尝试，重启服务后重新探测。封装复制不需要硬件探测。

镜像自带 OpenCV（`vision` 依赖组）和头像取景与比对用的人脸模型（YuNet 为 MIT，SFace 为 Apache-2.0，许可证在同一目录），位于 `/app/tools/face-models`，由 `PEACH_FACE_MODELS` 指向，容器不联网也能用。构建镜像时按固定地址下载并校验 SHA-256。

镜像包含 Debian FFmpeg、Mesa VAAPI 驱动与 `vainfo`。AMD 宿主机需加载 `amdgpu` 和对应固件，并提供 `/dev/dri/renderD*`。Intel 需在自定义镜像内安装适配设备的 VAAPI 驱动；Mesa 驱动不代表已覆盖全部 Intel GPU。arm64 镜像包含同一组软件包；ARM SoC 的硬件编码通常还依赖厂商的内核驱动与 VAAPI 实现，镜像不随附，需在目标机器验证。将下面的条目合入 Compose 的 `services.peach`，设备路径按宿主机调整：

```yaml
    devices:
      - /dev/dri/renderD128:/dev/dri/renderD128
    group_add:
      - "${PEACH_RENDER_GID}"
    environment:
      PEACH_TRANSCODE_BACKEND: auto
      PEACH_VAAPI_DEVICE: /dev/dri/renderD128
```

`PEACH_RENDER_GID` 填宿主机渲染节点所属组的数字 GID。容器以 UID 1000 运行，需要该节点的读写权限；只透传设备但没有组权限仍会初始化失败。Unraid 在容器设置中添加同样的设备映射、环境变量和额外组，无需开启特权模式。默认 Compose 不挂设备，无 GPU 的机器可直接运行。

| 环境变量 | 值与用途 |
| --- | --- |
| `PEACH_TRANSCODE_BACKEND` | 默认 `auto`；可设为 `vaapi`、`nvenc` 或 `software`。指定硬件后端仍会验证能否编码，失败回退软件。 |
| `PEACH_VAAPI_DEVICE` | 指定容器内渲染节点；留空时按名称依次探测 `/dev/dri/renderD*`，使用第一个可用节点。 |

VAAPI 尝试顺序为硬件解码加 `scale_vaapi` / `h264_vaapi`、软件解码加 `hwupload` / `h264_vaapi`、`libx264`。解码能力还受 GPU 型号、片源编码与位深限制；硬件编码可用不能证明每个片源都能硬件解码。NVIDIA 透传还需宿主机驱动及 NVIDIA Container Toolkit，Mesa 不提供 CUDA / NVENC。

出现 CPU 高占用时，先查看日志中的「转码硬件后端」和「转码硬件探测失败」。完成日志「整片转码完成」或「HLS 分片完成」后的 `vaapi` 表示使用硬件解码和编码，`vaapi-encode` 表示软件解码加硬件编码，`libx264` 表示软件转码。再在同一容器、同一用户下检查设备权限与 `vainfo --display drm --device /dev/dri/renderD128` 的结果。`No VA display found` 需结合驱动和设备权限排查，不能仅凭节点存在排除权限问题。

`ffmpeg -hwaccels` 列出构建支持的硬件加速方式，`ffmpeg -h decoder=h264` 等列出对应解码器支持的硬件设备。VAAPI 解码通过普通解码器的硬件加速接口启用，`ffmpeg -decoders` 中没有名为 `vaapi` 的解码器不代表缺少 VAAPI 解码支持。参数含义见 [FFmpeg 官方文档](https://ffmpeg.org/ffmpeg.html#Advanced-Video-options)。目标 GPU 的实际解码、GPU 占用与播放效果需要在部署机器上验证。

## amane 刮削站点

镜像随附按 `tools/amane-bridge/uv.lock` 构建的 amane `0.20.0` 独立环境，位于 `/app/amane-runtime`，通过 `PEACH_AMANE_RUNTIME` 定位。全新 `/data` 卷可直接调用刮削站点，无需在首次运行时安装依赖。桥通过独立子进程调用，站点请求仍使用所选连接方式。

运行环境与镜像一起更新，`manifest.json` 记录固定 revision、版本和解释器相对路径。镜像构建阶段与最终运行阶段都会执行 `bridge.py --self-check`，离线载入运行时与全部 Peach 开放站点类。此检查不发站点请求；来源网络是否可达需在部署环境验证。环境损坏时重新拉取并创建对应镜像，保留 `/data` 卷。

`/app/amane-runtime` 同时包含 GPL-3.0 许可证、来源说明和 `amane-corresponding-source.zip`；归档包含固定 revision 的完整 amane 源码、Peach 桥、锁文件、构建入口，以及 zhconv 和 certifi 的固定源码。依赖许可证保留在独立环境内。

## 导出诊断报告

Docker 部署使用网页「Peach 配置 → 系统诊断 → 下载诊断报告」，无需桌面托盘。已登录的私有网络客户端可以下载；通过反向代理或公网访问时，改用容器内导出。

网页不可用时，在 Compose 文件所在目录执行下面的命令，在正在运行的 `peach` 服务容器中生成脱敏 ZIP。目标文件已存在会拒绝覆盖；再次导出时同时更换两条命令中的文件名。

```text
docker compose exec peach python -m peach.support_bundle /tmp/peach-support.zip
```

将 ZIP 复制到当前目录：

```text
docker compose cp peach:/tmp/peach-support.zip ./peach-support.zip
```

报告包含版本、运行日志和任务状态，不包含数据库、媒体或配置原文。发送前检查内容，脱敏范围见 [日志与诊断](DIAGNOSTICS.md)。容器不连接宿主机桌面托盘；管理与诊断入口均在网页和 CLI 中提供。

## 更新与范围

更新镜像并重建容器时保留 `/data`。数据库有待执行迁移时，先备份并按 [运行与配置](OPERATIONS.md) 的迁移流程处理；Docker 启动入口不会自动迁移已有数据库。健康检查使用 `/healthz?ready=1`。

Linux 容器不读取用户电脑上的浏览历史，也不包含交互式浏览器；需要本机浏览器完成验证的来源在此环境不可用。CloudDrive2 的真实账号登录、网盘流量与 NAS 实机挂载需在目标机器验证。

## 镜像发布

构建使用官方 Python `3.15.1-slim-trixie` 与 uv `0.13.5`，应用依赖与 amane 独立依赖分别由各自的 `uv.lock` 锁定。应用以非 editable 包安装，包含 Web 与迁移资源。镜像以 UID/GID 1000 运行，内置 FFmpeg、FFprobe、OpenSSL 和 amane 独立环境。uv 与 Git 只用于构建阶段。构建上下文只允许代码与资源，不包含工作树、媒体、真实 Peach 数据库或凭据。

两个架构各自在自己的 runner 上构建一次，同时产出加载进本机 Docker 供冒烟的 `type=docker` 归档与供发布用的 `type=oci` 布局，归档加载后删除；标签只在发布时出现：`docker buildx imagetools create` 从两个布局合成索引，打成版本号与 `edge`，推送后再从 registry 读回逐个平台核对。GitHub Actions 的 `Docker` 工作流只构建并运行两个架构的临时卷验收，不推送；版本号与 `edge` 由 `release.yml` 在 GitHub Release 成功后推送。GitLab 的 `docker-build` 在 Windows Docker Desktop runner 上构建并验收两个平台，arm64 经 Docker Desktop 自带的 QEMU 运行。仓库变量 `DOCKERHUB_USERNAME` 和机密 `DOCKERHUB_TOKEN` 用于 Docker Hub 发布；机密只保存在 GitHub 和 GitLab 的受保护变量中，不进入项目文件。未配置发布身份时仍执行构建验收。镜像身份、演练产物与发布核对见 [ADR-0129](adr/0129-multiarch-images-build-per-architecture.md)。
