param([ValidateSet('verify', 'build', 'smoke', 'docker-build', 'docker-push')][string]$Mode = 'verify',
      # verify 默认全量；changed 只跑改动域与平台敏感用例，供主线推送使用（ADR-0119）。
      [ValidateSet('full', 'changed')][string]$Coverage = 'full')
$ErrorActionPreference = 'Stop'

function Assert-Exit([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw $Message }
}

# 标签流水线核对标签与版本号一致；版本提交的演练（ADR-0121）还没有标签，由流水线规则保证是版本提交。
function Assert-ReleaseRef([string]$Version) {
    if ($env:PEACH_RELEASE_REHEARSAL -eq '1') { return }
    if ($env:CI_COMMIT_TAG -ne "v$Version") { throw 'Tag 与版本不一致' }
}

if ($Mode -eq 'verify' -or $Mode -eq 'build') {
    uv sync --locked --all-extras --python 3.14
    Assert-Exit '锁定依赖安装失败'
    uv pip check --python .venv/Scripts/python.exe
    Assert-Exit '依赖检查失败'
    & .venv/Scripts/python.exe -X utf8 scripts/npm_ci.py --ignore-scripts
    Assert-Exit '根目录 npm 安装失败'
    & .venv/Scripts/python.exe -X utf8 scripts/npm_ci.py --prefix frontend
    Assert-Exit '前端依赖安装失败'
}

if ($Mode -eq 'verify') {
    $env:PEACH_E2E_CHROME = Join-Path $env:ProgramFiles 'Google/Chrome/Application/chrome.exe'
    $Plan = @('full')
    if ($Coverage -eq 'changed') {
        $Plan = @(& .venv/Scripts/python.exe -X utf8 scripts/ci_scope.py --platform windows)
        Assert-Exit '无法判定主线选测范围'
    }
    if ($Plan[0] -eq 'full') {
        & ./scripts/test.ps1 -Scope full -Resume
        Assert-Exit 'Windows 全量回归未通过'
        exit 0
    }
    & ./scripts/test.ps1 -Scope auto -Base $Plan[0]
    Assert-Exit 'Windows 改动域回归未通过'
    & ./scripts/test.ps1 -Focus @($Plan | Select-Object -Skip 1)
    Assert-Exit 'Windows 平台敏感用例未通过'
    exit 0
}

if ($Mode -eq 'build') {
    $LocalCompiler = Join-Path $env:LOCALAPPDATA 'Programs/Inno Setup 6/ISCC.exe'
    if (-not $env:PEACH_ISCC -and (Test-Path -LiteralPath $LocalCompiler)) { $env:PEACH_ISCC = $LocalCompiler }
    $Version = (& .venv/Scripts/python.exe -c 'from peach import __version__; print(__version__)').Trim()
    Assert-Exit '无法读取发布版本'
    Assert-ReleaseRef $Version
    $Changes = git status --porcelain --untracked-files=all
    Assert-Exit '无法核对提交状态'
    if ($Changes) { throw '构建必须使用干净提交' }
    & ./scripts/fetch_cloudflared.ps1 -OutputPath build/cloudflared.exe
    Assert-Exit 'cloudflared 校验失败'
    & ./scripts/test.ps1 -Scope packaging -Focus test_desktop_installer
    Assert-Exit '安装目录保护回归失败'
    & ./scripts/build_windows.ps1 -Installer -RequireReporting -CloudflaredPath build/cloudflared.exe -FFmpegPath build/ffmpeg-windows/ffmpeg -UntruncPath build/untrunc-windows/untrunc
    Assert-Exit 'Windows 构建失败'
    New-Item -ItemType Directory -Path release -Force | Out-Null
    & .venv/Scripts/python.exe scripts/package_windows_release.py --source dist/Peach --output release --version $Version --lite-installer
    Assert-Exit '发行包拆分失败'
    & .venv/Scripts/python.exe scripts/update_compat.py --packages release --version $Version
    Assert-Exit '支持范围内的旧版客户端装不上这一版'
    Copy-Item -LiteralPath "dist/Peach-$Version-windows-x64-setup.exe" -Destination release/
    Copy-Item -LiteralPath dist/ffmpeg-corresponding-source.zip, dist/untrunc-corresponding-source.zip -Destination release/
    Copy-Item -LiteralPath scripts/smoke_desktop.py, scripts/smoke_update_tasks.py, scripts/smoke_split_update.py, scripts/ci_windows.ps1 -Destination release/
    & .venv/Scripts/python.exe scripts/changelog.py --notes $Version --to release/RELEASE_NOTES.md
    Assert-Exit '缺少发布说明'
    Add-Content -LiteralPath release/RELEASE_NOTES.md -Value (Get-Content -LiteralPath release/DOWNLOADS.md -Raw -Encoding utf8) -Encoding utf8
    $Version | Set-Content -LiteralPath release/version.txt -Encoding utf8
    exit 0
}

if ($Mode -eq 'docker-build' -or $Mode -eq 'docker-push') {
    # Linux 平台 Runner 不提供 Docker 守护进程；两个架构在本机 Docker Desktop 各自构建、
    # 加载、验收，arm64 经 QEMU 运行。布局留在本机，等 publish-release 成功后再合成索引推送。
    $Match = Select-String -LiteralPath src/peach/__init__.py -Pattern '^__version__ = "([^"]+)"'
    if (-not $Match) { throw '无法读取发布版本' }
    $Version = $Match.Matches[0].Groups[1].Value
    Assert-ReleaseRef $Version
    $Endpoint = (docker context inspect --format '{{.Endpoints.docker.Host}}')
    Assert-Exit '需要运行 Docker Desktop'
    $Receipt = 'build/docker/receipt.json'
    $Recorded = 'build/ci-artifacts/docker-image.json'
    # 标签流水线是全新检出，演练验收过的布局按项目与提交留在本机 CI 构建根。
    if (-not $env:CI_BUILDS_DIR) { throw '缺少 CI_BUILDS_DIR，无法保存镜像布局' }
    $Layouts = Join-Path (Join-Path $env:CI_BUILDS_DIR '.peach-docker-layouts') `
        (Join-Path $env:CI_PROJECT_ID $env:CI_COMMIT_SHA)
    if ($Mode -eq 'docker-push') {
        $CandidateReceipt = Get-Content -LiteralPath build/ci-artifacts/release-candidate.json -Raw -Encoding utf8 | ConvertFrom-Json
        if ($CandidateReceipt.target.pipeline -ne $env:CI_PIPELINE_ID -or $CandidateReceipt.target.commit -ne $env:CI_COMMIT_SHA) { throw '镜像晋级回执不属于当前发布' }
    }
}

if ($Mode -eq 'docker-build') {
    $Revision = (git rev-parse HEAD).Trim()
    Assert-Exit '无法读取提交'
    if ((Test-Path -LiteralPath build/ci-artifacts/docker-release-resume.json) -and (Get-Content -LiteralPath build/ci-artifacts/docker-release-resume.json -Raw | ConvertFrom-Json).restored) {
        & (Get-Command python.exe).Source -X utf8 scripts/docker_image.py verify --receipt $Recorded --layouts $Layouts
        Assert-Exit '已登记的镜像布局与回执不一致，请重新执行演练'
        exit 0
    }
    if (Test-Path -LiteralPath $Layouts) { Remove-Item -LiteralPath $Layouts -Recurse -Force }
    New-Item -ItemType Directory -Path $Layouts -Force | Out-Null
    docker buildx inspect peach-docker *> $null
    if ($LASTEXITCODE -ne 0) { docker buildx create --name peach-docker --driver docker-container --use | Out-Null }
    else { docker buildx use peach-docker | Out-Null }
    Assert-Exit '无法准备多架构构建器'
    foreach ($Platform in @('linux/amd64', 'linux/arm64')) {
        & (Get-Command python.exe).Source -X utf8 scripts/docker_image.py build --destination $Layouts `
            --platform $Platform --version $Version --commit $Revision `
            --build-arg PEACH_SENTRY_DSN --build-arg PEACH_LIBRARY_SENTRY_DSN --build-arg PEACH_REQUIRE_REPORTING=1
        Assert-Exit "$Platform 镜像构建失败"
        & (Get-Command python.exe).Source -X utf8 scripts/smoke_docker.py "peach-build:$($Platform.Split('/')[1])" --platform $Platform
        Assert-Exit "$Platform 镜像验收失败"
    }
    docker run --rm --platform linux/amd64 --entrypoint python 'peach-build:amd64' -c 'from peach import library_sharing, remote_reporting, reporting_identity; assert remote_reporting.dsn() and library_sharing.target() and reporting_identity.distribution() == ''docker'''
    Assert-Exit '镜像内 Sentry DSN 或分发标记缺失'
    & (Get-Command python.exe).Source -X utf8 scripts/docker_image.py record --receipt (Join-Path $Layouts 'amd64.json') --receipt (Join-Path $Layouts 'arm64.json') --version $Version --commit $Revision --destination $Receipt
    Assert-Exit '无法合成多架构发布回执'
    $Payload = Get-Content -LiteralPath $Receipt -Raw -Encoding utf8 | ConvertFrom-Json
    $Payload | Add-Member -NotePropertyName pipeline -NotePropertyValue $env:CI_PIPELINE_ID -Force
    New-Item -ItemType Directory -Path build/ci-artifacts -Force | Out-Null
    $Payload | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $Recorded -Encoding utf8
    exit 0
}

if ($Mode -eq 'docker-push') {
    if (-not $env:DOCKERHUB_USERNAME -or -not $env:DOCKERHUB_TOKEN) { throw '缺少 Docker Hub 发布变量' }
    # 标签流水线不下载演练布局：同一台 runner 上按回执核对已验收的布局，再合成索引推送。
    $Expected = $CandidateReceipt.candidate.docker
    if ($Expected.commit -ne $env:CI_COMMIT_SHA -or $Expected.version -ne $Version) {
        throw '候选镜像不属于当前发布提交与版本'
    }
    $ExpectedPath = 'build/docker/candidate.json'
    New-Item -ItemType Directory -Path build/docker -Force | Out-Null
    $Expected | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ExpectedPath -Encoding utf8
    & (Get-Command python.exe).Source -X utf8 scripts/docker_image.py verify --receipt $ExpectedPath --layouts $Layouts
    Assert-Exit '本机没有演练验收过的镜像布局；重试 docker-build 后再推送'
    # 登录写进作业专用配置目录，不改动宿主用户的 Docker 登录。
    $SavedConfig, $SavedHost = $env:DOCKER_CONFIG, $env:DOCKER_HOST
    $env:DOCKER_HOST = $Endpoint
    $env:DOCKER_CONFIG = Join-Path ([IO.Path]::GetTempPath()) ('peach-docker-config-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $env:DOCKER_CONFIG | Out-Null
    try {
        $env:DOCKERHUB_TOKEN | docker login --username $env:DOCKERHUB_USERNAME --password-stdin
        Assert-Exit 'Docker Hub 登录失败'
        & (Get-Command python.exe).Source -X utf8 scripts/docker_image.py publish --receipt $ExpectedPath --layouts $Layouts --tag $Version --tag edge
        Assert-Exit '推送多架构镜像失败'
    } finally {
        docker logout | Out-Null
        Remove-Item -LiteralPath $env:DOCKER_CONFIG -Recurse -Force -ErrorAction SilentlyContinue
        $env:DOCKER_CONFIG, $env:DOCKER_HOST = $SavedConfig, $SavedHost
    }
    Remove-Item -LiteralPath $Layouts -Recurse -Force -ErrorAction SilentlyContinue
    exit 0
}

$Version = (Get-Content -LiteralPath release/version.txt -Raw -Encoding utf8).Trim()
$Packages = (Resolve-Path release).Path
$ValidationScripts = (Resolve-Path (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts')).Path
if (-not (Test-Path -LiteralPath (Join-Path $ValidationScripts 'build_amane_runtime.py'))) {
    throw '独立制品验收缺少同一检出下的辅助模块'
}
$PreviousValidationPythonPath = $env:PYTHONPATH
$Evidence = Join-Path ([IO.Path]::GetTempPath()) ('peach-release-smoke-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Evidence | Out-Null
$Python = (Get-Command python.exe).Source
$Key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{02D9748A-78F6-4866-AC75-918177194DAB}_is1'
$Menu = Join-Path $env:APPDATA 'Microsoft/Windows/Start Menu/Programs/Peach.lnk'
if ((Test-Path -LiteralPath $Key) -or (Test-Path -LiteralPath $Menu)) { throw '现有 Peach 安装登记需要保留，不能覆盖安装验收' }
Push-Location $Evidence
try {
    # 验收宿主使用同一检出的辅助模块；被测应用清除 PYTHONPATH。
    $env:PYTHONPATH = $ValidationScripts
    Expand-Archive -LiteralPath "$Packages/Peach-$Version-windows-x64.zip" -DestinationPath candidate
    & $Python "$Packages/smoke_desktop.py" candidate/Peach/Peach.exe
    Assert-Exit '完整包运行验收失败'
    & $Python "$Packages/smoke_split_update.py" --root $Packages --version $Version --destination split-candidate --prepare-with-app
    Assert-Exit '拆分包重建失败'
    & $Python "$Packages/smoke_desktop.py" split-candidate/Peach/Peach.exe
    Assert-Exit '拆分包运行验收失败'
    foreach ($Suffix in @('-setup.exe', '-lite-setup.exe')) {
        $Installed = Join-Path $Evidence ('安装验收/自定义目录' + $Suffix)
        $Setup = Start-Process -FilePath "$Packages/Peach-$Version-windows-x64$Suffix" -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$Installed`"") -WindowStyle Hidden -Wait -PassThru
        if ($Setup.ExitCode -ne 0) { throw "安装包退出码 $($Setup.ExitCode)" }
        if (-not (Test-Path -LiteralPath $Key) -or -not (Test-Path -LiteralPath $Menu)) { throw '缺少安装登记或开始菜单项' }
        & $Python "$Packages/smoke_desktop.py" (Join-Path $Installed 'Peach.exe')
        Assert-Exit '安装包运行验收失败'
        Start-Process -FilePath (Join-Path $Installed 'unins000.exe') -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -WindowStyle Hidden -Wait
        $Deadline = (Get-Date).AddSeconds(120)
        while ((Test-Path -LiteralPath $Installed) -and (Get-Date) -lt $Deadline) { Start-Sleep -Seconds 1 }
        if ((Test-Path -LiteralPath $Installed) -or (Test-Path -LiteralPath $Key) -or (Test-Path -LiteralPath $Menu)) { throw '卸载验收未通过' }
    }
    & $Python "$Packages/smoke_update_tasks.py" candidate/Peach/Peach.exe --installer "$Packages/Peach-$Version-windows-x64-setup.exe" --apply
    Assert-Exit '任务保护与安装恢复验收失败'
} finally {
    $env:PYTHONPATH = $PreviousValidationPythonPath
    Pop-Location
    Write-Host "独立制品验收目录：$Evidence"
}
