# Transcribe to Learn (TTL) - MSIX 패키지 빌드
#
# 사용법:
#   powershell -ExecutionPolicy Bypass -File tools/make_msix.ps1 -Version 1.1.1.0
#       -> 스토어 제출본. Partner Center Identity, 서명 없음.
#          (Microsoft 가 자기 인증서로 다시 서명하므로 우리 서명은 필요 없다)
#   powershell -ExecutionPolicy Bypass -File tools/make_msix.ps1 -Version 1.1.1.0 -Install
#       -> 로컬 확인본. 자체 서명 인증서로 서명하고 이 PC에 사이드로드한다.
#
# 두 판은 매니페스트의 Publisher 가 다르다. 스토어 Publisher 는
# CN=<GUID> 라 자체 서명 인증서를 만들 수 없고, 서명 주체와 매니페스트
# Publisher 가 다르면 Windows 가 설치를 거부한다. 그래서 -Install 일 때만
# 자체 서명용 Identity 로 바꿔 넣는다. 나머지(코드/자산/버전)는 완전히 같다.
#
# 먼저 dist\STT_KOR\ 가 있어야 한다 (build_exe.ps1 또는 PyInstaller).

param(
    [string]$Version = "1.1.1.0",
    [switch]$Install,
    [string]$Publisher   # 자체 서명 테스트용 주체를 직접 지정할 때만. 기본값 아래.
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

# ---- 0) Identity 결정 -----------------------------------------------------
# Partner Center > 제품 > 제품 ID (Store ID 9NBSRBP82NT8) 에서 발급된 값.
# Name 의 가운데 "to" 는 소문자다. 한 글자라도 다르면 업로드가 거부된다.
$StoreIdentityName = "NombarHwan.TranscribetoLearn"
$StorePublisher    = "CN=4A62FD04-10BA-49EF-89B6-10CAED11DC9E"
$PublisherDisplay  = "NombarHwan"

if ($Install) {
    # 사이드로드용. Identity Name 은 그대로 두고 Publisher 만 자체 서명 주체로.
    if (-not $Publisher) { $Publisher = "CN=NombarHwan" }
    $identityName = $StoreIdentityName
    $mode = "로컬 사이드로드용"
} else {
    if ($Publisher -and $Publisher -ne $StorePublisher) {
        throw "-Publisher 는 -Install 과 함께 쓸 때만 의미가 있습니다. 스토어 제출본의 Publisher 는 $StorePublisher 로 고정입니다."
    }
    $Publisher = $StorePublisher
    $identityName = $StoreIdentityName
    $mode = "스토어 제출용"
}
Write-Host "빌드 종류: $mode  (Publisher $Publisher)"

if ($Version -notmatch '^\d+\.\d+\.\d+\.0$') {
    throw "Version 은 네 자리여야 하고 마지막 자리는 0 이어야 합니다 (스토어 요구사항): $Version"
}
if (-not (Test-Path "dist\STT_KOR\STT_KOR.exe")) {
    throw "dist\STT_KOR\STT_KOR.exe 가 없습니다. 먼저 build_exe.ps1 을 돌리세요."
}

# ---- 1) makeappx / signtool 확보 -----------------------------------------
# 전체 Windows SDK(수 GB) 대신 도구만 든 NuGet 패키지를 쓴다.
$toolRoot = Join-Path $env:LOCALAPPDATA "TTL-build\sdk"
# NuGet 패키지에는 arm64/x86/x64 가 모두 들어 있다. x64 만 골라야 한다
# (arm64 를 집으면 "not a valid application for this OS platform" 으로 죽는다).
function Find-Tool($root, $name) {
    Get-ChildItem $root -Recurse -Filter $name -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -like "*\x64\*" } |
        Select-Object -First 1 -ExpandProperty FullName
}
$makeappx = Find-Tool $toolRoot "makeappx.exe"
if (-not $makeappx) {
    Write-Host "Windows SDK 빌드 도구를 내려받는 중..."
    New-Item -ItemType Directory -Force $toolRoot | Out-Null
    $idx = Invoke-RestMethod "https://api.nuget.org/v3-flatcontainer/microsoft.windows.sdk.buildtools/index.json"
    # preview 가 아닌 최신 안정 버전
    $ver = ($idx.versions | Where-Object { $_ -notmatch '-' } | Select-Object -Last 1)
    $nupkg = Join-Path $toolRoot "sdk.zip"
    Invoke-WebRequest "https://api.nuget.org/v3-flatcontainer/microsoft.windows.sdk.buildtools/$ver/microsoft.windows.sdk.buildtools.$ver.nupkg" -OutFile $nupkg
    Expand-Archive $nupkg -DestinationPath $toolRoot -Force
    Remove-Item $nupkg
    $makeappx = Find-Tool $toolRoot "makeappx.exe"
}
$binDir   = Split-Path $makeappx -Parent
$signtool = Join-Path $binDir "signtool.exe"
$makepri  = Join-Path $binDir "makepri.exe"
Write-Host "도구: $binDir"

# ---- 2) 패키지 폴더 구성 --------------------------------------------------
$stage = "msix_out\stage"
Remove-Item -Recurse -Force "msix_out" -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$stage\Assets" | Out-Null

Copy-Item "dist\STT_KOR\*" $stage -Recurse
# 매니페스트가 참조하는 시각 자산. 배율/targetsize 변형은 makepri 가 색인한다.
Copy-Item "assets\*.png" "$stage\Assets"

(Get-Content "packaging\AppxManifest.xml" -Raw -Encoding UTF8).
    Replace("{VERSION}", $Version).
    Replace("{IDENTITY_NAME}", $identityName).
    Replace("{PUBLISHER}", $Publisher).
    Replace("{PUBLISHER_DISPLAY}", $PublisherDisplay) |
    Set-Content "$stage\AppxManifest.xml" -Encoding UTF8
Write-Host "구성 완료: $stage (버전 $Version)"

# ---- 3) 리소스 색인 (배율별 아이콘을 Windows 가 고르게 해 준다) -----------
Push-Location $stage
& $makepri createconfig /cf priconfig.xml /dq ko-KR_en-US /o | Out-Null
& $makepri new /pr . /cf priconfig.xml /of resources.pri /o | Out-Null
Remove-Item priconfig.xml
Pop-Location
if (-not (Test-Path "$stage\resources.pri")) { throw "resources.pri 생성 실패" }

# ---- 4) 패키징 ------------------------------------------------------------
$suffix = if ($Install) { "-sideload" } else { "-store" }
$msix = "msix_out\TranscribeToLearn-$Version$suffix.msix"
& $makeappx pack /d $stage /p $msix /o
if ($LASTEXITCODE -ne 0) { throw "makeappx 실패 (exit $LASTEXITCODE)" }
Write-Host ""
Write-Host "패키지 생성: $msix"

if (-not $Install) {
    Write-Host ""
    Write-Host "스토어 제출은 이 .msix 를 서명 없이 그대로 올리면 됩니다."
    Write-Host "  Partner Center > 제품 > 제출 > 패키지 에 업로드"
    Write-Host "이 PC 에서 먼저 확인하려면 -Install 을 붙여 다시 실행하세요."
    Write-Host "(-Install 은 Publisher 가 다른 별도 패키지를 만든다. 제출본이 아니다.)"
    return
}

# ---- 5) 로컬 확인용 자체 서명 + 설치 --------------------------------------
# 스토어 제출에는 필요 없다. 사이드로드하려면 Windows 가 서명을 요구할 뿐이다.
$cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Subject -eq $Publisher } | Select-Object -First 1
if (-not $cert) {
    Write-Host "테스트용 자체 서명 인증서 생성: $Publisher"
    $cert = New-SelfSignedCertificate -Type Custom -Subject $Publisher `
        -KeyUsage DigitalSignature -FriendlyName "TTL sideload test" `
        -CertStoreLocation "Cert:\CurrentUser\My" `
        -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}")
}
$pfx = "msix_out\test.pfx"
$pw = ConvertTo-SecureString -String "ttl-local" -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath $pfx -Password $pw | Out-Null

& $signtool sign /fd SHA256 /f $pfx /p "ttl-local" $msix
if ($LASTEXITCODE -ne 0) { throw "서명 실패 (exit $LASTEXITCODE)" }

# 자체 서명이라 이 PC 가 인증서를 신뢰해야 설치된다(신뢰된 사람 저장소).
$pub = "msix_out\test.cer"
Export-Certificate -Cert $cert -FilePath $pub | Out-Null
Write-Host ""
Write-Host "설치하려면 관리자 PowerShell 에서 인증서를 신뢰시킨 뒤 설치하세요:"
Write-Host "  Import-Certificate -FilePath `"$((Resolve-Path $pub).Path)`" -CertStoreLocation Cert:\LocalMachine\TrustedPeople"
Write-Host "  Add-AppxPackage `"$((Resolve-Path $msix).Path)`""
