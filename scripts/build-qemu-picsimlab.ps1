param(
    [string]$MsysRoot = 'C:\msys64',
    [string]$SourceDirectory = 'vendor\qemu-picsimlab-src',
    [string]$PackageDirectory = 'vendor\qemu-picsimlab'
)

$ErrorActionPreference = 'Stop'
$Repository = 'https://github.com/lcgamboa/qemu.git'
$Revision = 'e39508f79cb0681bbe15cfcebd40a3f45cdc3c48'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Source = Join-Path $Root $SourceDirectory
$Package = Join-Path $Root $PackageDirectory
$Shell = Join-Path $MsysRoot 'msys2_shell.cmd'

if (-not (Test-Path $Shell -PathType Leaf)) {
    throw "MSYS2 was not found at $MsysRoot. Install its UCRT64 GCC, Python, Meson, Ninja, GLib, Pixman, libgcrypt, libslirp, zlib, Flex, Bison, Git, Make, and Patch packages first."
}

if (-not (Test-Path (Join-Path $Source '.git') -PathType Container)) {
    New-Item -ItemType Directory -Force (Split-Path $Source) | Out-Null
    git clone --branch picsimlab-esp32 --single-branch $Repository $Source
}

git -C $Source fetch origin picsimlab-esp32
git -C $Source reset --hard $Revision
git -C $Source clean -fdx

$Patches = @(
    Join-Path $Root 'patches\qemu-picsimlab\0001-classic-esp32-safe-host-io.patch'
    Join-Path $Root 'patches\qemu-picsimlab\0002-windows-xtensa-dll.patch'
)
foreach ($Patch in $Patches) {
    git -C $Source apply --check $Patch
    git -C $Source apply $Patch
}

$SourceUnix = (& $Shell -defterm -no-start -ucrt64 -c "cygpath -u '$Source'").Trim()
& $Shell -defterm -no-start -ucrt64 -c "cd '$SourceUnix' && ./configure --target-list=xtensa-softmmu --without-default-features --enable-system --enable-tcg --enable-gcrypt --enable-pixman --enable-slirp --disable-werror --disable-docs --disable-relocatable --static && ninja -C build -j8 libqemu-xtensa.dll"
if ($LASTEXITCODE -ne 0) { throw 'PICSimLab QEMU build failed.' }

$Bin = Join-Path $Package 'bin'
$Rom = Join-Path $Package 'share\qemu'
New-Item -ItemType Directory -Force $Bin, $Rom | Out-Null
Copy-Item (Join-Path $Source 'build\libqemu-xtensa.dll') (Join-Path $Bin 'libqemu-xtensa.dll') -Force
Copy-Item (Join-Path $Source 'pc-bios\esp32-v3-rom.bin'), (Join-Path $Source 'pc-bios\esp32-v3-rom-app.bin') $Rom -Force
Copy-Item (Join-Path $Source 'COPYING') (Join-Path $Package 'COPYING') -Force

$DllUnix = (& $Shell -defterm -no-start -ucrt64 -c "cygpath -u '$(Join-Path $Bin 'libqemu-xtensa.dll')'").Trim()
& $Shell -defterm -no-start -ucrt64 -c "strip --strip-unneeded '$DllUnix'"
if ($LASTEXITCODE -ne 0) { throw 'PICSimLab QEMU DLL stripping failed.' }

Get-ChildItem $Package -Recurse -File | Select-Object FullName, Length
