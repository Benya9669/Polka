[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [string] $Source,

    [Parameter(Mandatory)]
    [string] $Drive,

    [switch] $Apply,

    [switch] $Eject
)

$ErrorActionPreference = 'Stop'

function Get-EjectTarget($LogicalDisk) {
    $partition = @(Get-CimAssociatedInstance -InputObject $LogicalDisk -Association Win32_LogicalDiskToPartition)
    if ($partition.Count -ne 1) {
        throw 'Windows не удалось однозначно определить раздел для безопасного извлечения.'
    }
    $device = @(Get-CimAssociatedInstance -InputObject $partition[0] -Association Win32_DiskDriveToDiskPartition)
    if ($device.Count -ne 1 -or [string]::IsNullOrWhiteSpace($device[0].PNPDeviceID)) {
        throw 'Windows не удалось однозначно определить устройство для безопасного извлечения.'
    }
    $disk = Get-Disk -Number $device[0].Index
    if ($null -eq $disk -or $disk.IsBoot -or $disk.IsSystem) {
        throw 'Автоматическое извлечение этого диска недоступно.'
    }
    return $device[0].PNPDeviceID
}

function Request-DeviceEject([string] $DeviceId) {
    if (-not ('Polka.RemovableDisk' -as [type])) {
        Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
using System.Text;

namespace Polka {
    public static class RemovableDisk {
        [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
        public static extern uint CM_Locate_DevNodeW(out uint node, string deviceId, uint flags);

        [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
        public static extern uint CM_Request_Device_EjectW(uint node, out uint vetoType, StringBuilder vetoName,
                                                         uint nameLength, uint flags);
    }
}
'@
    }
    if ([string]::IsNullOrWhiteSpace($DeviceId)) {
        throw 'Идентификатор устройства отсутствует.'
    }
    [uint32] $node = 0
    $result = [Polka.RemovableDisk]::CM_Locate_DevNodeW([ref] $node, $DeviceId, 0)
    if ($result -ne 0) {
        throw "Windows не нашла устройство (код $result)."
    }
    [uint32] $veto = 0
    $reason = [System.Text.StringBuilder]::new(260)
    $result = [Polka.RemovableDisk]::CM_Request_Device_EjectW($node, [ref] $veto, $reason, $reason.Capacity, 0)
    if ($result -ne 0) {
        throw "Windows отклонила извлечение (код $result, причина $veto $reason). Закройте файлы и окна этого диска."
    }
}

if ($Eject -and -not $Apply) {
    throw 'Для извлечения после передачи укажите -Apply вместе с -Eject.'
}
if ($Drive -notmatch '^[A-Za-z]:\\?$') {
    throw 'Укажите букву подключённого носителя, например E:'
}

$driveLetter = $Drive.Substring(0, 2).ToUpperInvariant()
$volume = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$driveLetter'"
$usb = $false
if ($volume -and $volume.DriveType -eq 3 -and $driveLetter -ne $env:SystemDrive) {
    try {
        $disk = Get-Partition -DriveLetter ($driveLetter.Substring(0, 1)) | Get-Disk
        $usb = $disk.BusType -eq 'USB'
    } catch {
        $usb = $false
    }
}
if ($null -eq $volume -or ($volume.DriveType -ne 2 -and -not $usb)) {
    throw "$driveLetter не определён как съёмный носитель или USB-диск."
}

$ejectTarget = $null
if ($Eject -and -not $WhatIfPreference) {
    try {
        $ejectTarget = Get-EjectTarget $volume
    } catch {
        Write-Warning $_.Exception.Message
    }
}

$sourcePath = (Resolve-Path -LiteralPath $Source).ProviderPath
if (-not (Test-Path -LiteralPath $sourcePath -PathType Container)) {
    throw 'Укажите папку с экспортированными книгами.'
}
if ([System.IO.Path]::GetPathRoot($sourcePath).TrimEnd('\') -eq $driveLetter) {
    throw 'Папка экспорта должна находиться на другом диске.'
}

$rootBooks = Join-Path ($driveLetter + '\') 'Books'
$destination = Join-Path $rootBooks 'Polka'
foreach ($folder in @($rootBooks, $destination)) {
    if (Test-Path -LiteralPath $folder) {
        $attrs = (Get-Item -LiteralPath $folder).Attributes
        if (-not ($attrs -band [System.IO.FileAttributes]::Directory) -or
                ($attrs -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw "Недопустимая папка на устройстве: $folder"
        }
    }
}
$manifestPath = Join-Path $sourcePath 'manifest.json'
$entries = if (Test-Path -LiteralPath $manifestPath) {
    $manifestItem = Get-Item -LiteralPath $manifestPath
    if ($manifestItem.PSIsContainer -or ($manifestItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -or
            $manifestItem.Length -gt 1048576) {
        throw 'Недопустимый манифест экспорта.'
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.profile -ne 'sony-prs-600' -or $manifest.version -ne 1 -or
            @($manifest.files).Count -lt 1 -or @($manifest.files).Count -gt 25) {
        throw 'Копирование ожидает манифест экспорта Sony PRS-600.'
    }
    $names = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $manifest.files) {
        if ($entry.filename -notmatch '^book-[0-9]+\.(epub|pdf)$' -or $entry.sha256 -notmatch '^[a-f0-9]{64}$' -or
                -not $names.Add([string]$entry.filename)) {
            throw 'Манифест содержит недопустимую запись о книге.'
        }
        $file = Get-Item -LiteralPath (Join-Path $sourcePath $entry.filename)
        if ($file.PSIsContainer -or ($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -or
                $file.Length -ne $entry.size) {
            throw "Проверьте комплектность пакета: $($entry.filename)"
        }
        [pscustomobject]@{ File = $file; Expected = $entry.sha256 }
    }
} else {
    Get-ChildItem -LiteralPath $sourcePath -File -Filter '*.epub' | ForEach-Object {
        [pscustomobject]@{ File = $_; Expected = $null }
    }
}
$plan = foreach ($entry in $entries) {
    $file = $entry.File
    if ($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        continue
    }
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($entry.Expected -and $hash -ne $entry.Expected) {
        throw "Контрольная сумма не совпадает с манифестом: $($file.Name)"
    }
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($file.Name).Trim()
    if ($stem.Length -gt 80) { $stem = $stem.Substring(0, 80) }
    $target = Join-Path $destination ("$stem-" + $hash.Substring(0, 16) + $file.Extension.ToLowerInvariant())
    $existing = Get-Item -LiteralPath $target -ErrorAction SilentlyContinue
    $exists = $null -ne $existing
    $state = if (-not $exists) {
        'Копирование'
    } elseif ($existing.PSIsContainer -or ($existing.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        'Коллизия, пропуск'
    } elseif ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -eq $hash) {
        'Уже на устройстве'
    } else {
        'Коллизия, пропуск'
    }
    [pscustomobject]@{ State = $state; Source = $file.FullName; Target = $target; Hash = $hash }
}
$plan | Format-Table -Property State, Source, Target -AutoSize

if (-not $Apply) {
    return
}
$transferComplete = [bool] $plan
foreach ($item in $plan) {
    if ($item.State -eq 'Уже на устройстве') {
        continue
    }
    if ($item.State -ne 'Копирование' -or -not $PSCmdlet.ShouldProcess($item.Target, 'Скопировать книгу')) {
        $transferComplete = $false
        continue
    }
    [System.IO.Directory]::CreateDirectory($destination) | Out-Null
    $temporary = Join-Path $destination ('.polka-' + [guid]::NewGuid().ToString('n') + '.tmp')
    try {
        [System.IO.File]::Copy($item.Source, $temporary, $false)
        if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.Hash) {
            throw "Контрольная сумма копии не совпала: $($item.Source)"
        }
        [System.IO.File]::Move($temporary, $item.Target)
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary
        }
    }
}
if ($ejectTarget -and $transferComplete -and $PSCmdlet.ShouldProcess($driveLetter, 'Безопасно извлечь носитель')) {
    try {
        $currentVolume = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$driveLetter'"
        if (-not $currentVolume -or $currentVolume.VolumeSerialNumber -ne $volume.VolumeSerialNumber -or
                (Get-EjectTarget $currentVolume) -ne $ejectTarget) {
            throw 'За время передачи носитель по указанной букве диска изменился.'
        }
        Request-DeviceEject $ejectTarget
        Write-Output "Windows завершила безопасное извлечение $driveLetter."
        return
    } catch {
        Write-Warning $_.Exception.Message
    }
}
Write-Output 'Перед отключением используйте безопасное извлечение Windows.'

