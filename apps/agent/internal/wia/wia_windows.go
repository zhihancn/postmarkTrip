//go:build windows

// Package wia 通过 Windows Image Acquisition (WIA) 驱动扫描仪。
//
// 实现方式：**把一段 PowerShell 脚本写到临时文件，用 powershell -File 执行。**
//
// 为什么不是 cgo + COM：那需要 MinGW 工具链与 cgo，会让这个"单文件可执行程序"
// 变成"需要特定环境才能构建"的东西，违背 docs/ok/04 §3.4 的取舍。
// 代价是每次调用多几十毫秒的进程启动 —— 对一次手动触发的扫描完全无所谓。
package wia

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/zhihancn/postmarkTrip/apps/agent/internal/scanner"
)

// listScript 枚举 WIA 设备。
//
// 注意：脚本内不含反引号（Go 原始字符串字面量的定界符），
// 也不使用 PowerShell 的反引号续行。
const listScript = `
$ErrorActionPreference = 'Stop'
$dm = New-Object -ComObject WIA.DeviceManager
$list = New-Object System.Collections.ArrayList
foreach ($info in $dm.DeviceInfos) {
  if ($info.Type -ne 1) { continue }
  $name = ''
  $desc = ''
  foreach ($p in $info.Properties) {
    if ($p.Name -eq 'Name') { $name = [string]$p.Value }
    if ($p.Name -eq 'Description') { $desc = [string]$p.Value }
  }
  $null = $list.Add([pscustomobject]@{ id = [string]$info.DeviceID; name = $name; description = $desc })
}
if ($list.Count -eq 0) { Write-Output '[]'; exit 0 }
ConvertTo-Json -InputObject @($list) -Compress -Depth 4
`

// scanScript 执行一次扫描。
//
// 参数通过 -File 的具名参数传入，**不做字符串拼接** —— 避免注入，
// 也避免路径里有空格或中文时出问题。
const scanScript = `
param(
  [Parameter(Mandatory=$true)][string]$DeviceId,
  [Parameter(Mandatory=$true)][int]$Dpi,
  [Parameter(Mandatory=$true)][int]$Intent,
  [Parameter(Mandatory=$true)][string]$OutFile
)
$ErrorActionPreference = 'Stop'
$dm = New-Object -ComObject WIA.DeviceManager
$dev = $null
foreach ($info in $dm.DeviceInfos) {
  if ($info.Type -ne 1) { continue }
  if ($DeviceId -ne '' -and [string]$info.DeviceID -ne $DeviceId) { continue }
  $dev = $info.Connect()
  break
}
if ($null -eq $dev) { throw 'no matching WIA device' }
$item = $dev.Items.Item(1)
foreach ($p in $item.Properties) {
  switch ($p.Name) {
    'Horizontal Resolution' { $p.Value = $Dpi }
    'Vertical Resolution'   { $p.Value = $Dpi }
    'Current Intent'        { $p.Value = $Intent }
  }
}
$img = $item.Transfer()
$img.SaveFile($OutFile)
`

// Backend 是 WIA 后端。
type Backend struct {
	// Timeout 是单次外部命令的超时。扫描大图可能慢，所以给得比较宽。
	Timeout time.Duration
	// Shell 可覆盖（测试用），默认自动探测 powershell.exe / pwsh.exe。
	Shell string
}

// New 返回一个 WIA 后端。
func New() *Backend { return &Backend{Timeout: 120 * time.Second} }

func (b *Backend) Name() string { return "wia" }

func (b *Backend) shell() (string, error) {
	if b.Shell != "" {
		return b.Shell, nil
	}
	for _, cand := range []string{"powershell.exe", "pwsh.exe"} {
		if p, err := exec.LookPath(cand); err == nil {
			return p, nil
		}
	}
	return "", &scanner.ToolNotFoundError{
		Tool:    "powershell",
		Install: "Windows 10/11 自带 powershell.exe；若被移除，请安装 PowerShell 7（pwsh）",
	}
}

func (b *Backend) run(ctx context.Context, script string, args ...string) ([]byte, error) {
	shell, err := b.shell()
	if err != nil {
		return nil, err
	}

	dir, err := os.MkdirTemp("", "pmtrip-wia-")
	if err != nil {
		return nil, fmt.Errorf("创建临时目录失败：%w", err)
	}
	defer os.RemoveAll(dir)

	scriptPath := filepath.Join(dir, "wia.ps1")
	if err := os.WriteFile(scriptPath, []byte(script), 0o600); err != nil {
		return nil, fmt.Errorf("写入临时脚本失败：%w", err)
	}

	cctx, cancel := context.WithTimeout(ctx, b.Timeout)
	defer cancel()

	// -ExecutionPolicy Bypass：脚本是我们自己写的，且组策略可能禁用脚本执行
	full := append([]string{"-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
		"-File", scriptPath}, args...)
	cmd := exec.CommandContext(cctx, shell, full...)

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		return nil, fmt.Errorf("WIA 调用失败：%s", msg)
	}
	return stdout.Bytes(), nil
}

type wiaDevice struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Description string `json:"description"`
}

func (b *Backend) List(ctx context.Context) ([]scanner.Device, error) {
	out, err := b.run(ctx, listScript)
	if err != nil {
		return nil, err
	}
	trimmed := bytes.TrimSpace(out)
	if len(trimmed) == 0 || bytes.Equal(trimmed, []byte("[]")) {
		return []scanner.Device{}, nil
	}
	var raw []wiaDevice
	if err := json.Unmarshal(trimmed, &raw); err != nil {
		return nil, fmt.Errorf("解析 WIA 设备列表失败：%w", err)
	}

	devices := make([]scanner.Device, 0, len(raw))
	for _, d := range raw {
		name := strings.TrimSpace(d.Name)
		if name == "" {
			name = strings.TrimSpace(d.Description)
		}
		if name == "" {
			name = d.ID
		}
		devices = append(devices, scanner.Device{
			ID:   d.ID,
			Name: name,
			// WIA 的能力要连上设备后从 item 属性里读；这里给出保守的能力声明，
			// 真正常用的分辨率由用户在界面上选（默认 600）。
			Capabilities: scanner.Capabilities{
				Sources: []scanner.Source{scanner.SourceFlatbed},
				MaxDPIX: 1200, MaxDPIY: 1200,
				Color:   true,
				Formats: []scanner.Format{scanner.FormatJPEG, scanner.FormatPNG, scanner.FormatTIFF},
			},
		})
	}
	return devices, nil
}

func (b *Backend) Scan(ctx context.Context, req scanner.ScanRequest) (*scanner.Result, error) {
	if err := req.Normalize(); err != nil {
		return nil, err
	}

	dir, err := os.MkdirTemp("", "pmtrip-scan-")
	if err != nil {
		return nil, fmt.Errorf("创建临时目录失败：%w", err)
	}
	defer os.RemoveAll(dir)

	ext := map[scanner.Format]string{
		scanner.FormatJPEG: ".jpg",
		scanner.FormatPNG:  ".png",
		scanner.FormatTIFF: ".tif",
	}[req.Format]
	if ext == "" {
		ext = ".jpg"
	}
	outFile := filepath.Join(dir, "scan"+ext)

	// WIA 的 Current Intent：0 = 彩色/无意图，1 = 灰度，2 = 文本
	intent := 1
	if req.Color {
		intent = 0
	}

	if _, err := b.run(ctx, scanScript,
		"-DeviceId", req.DeviceID,
		"-Dpi", fmt.Sprint(req.DPI),
		"-Intent", fmt.Sprint(intent),
		"-OutFile", outFile,
	); err != nil {
		return nil, err
	}

	data, err := os.ReadFile(outFile)
	if err != nil {
		return nil, fmt.Errorf("读取扫描结果失败：%w", err)
	}
	if len(data) == 0 {
		return nil, fmt.Errorf("扫描返回了空文件")
	}

	mime := map[scanner.Format]string{
		scanner.FormatJPEG: "image/jpeg",
		scanner.FormatPNG:  "image/png",
		scanner.FormatTIFF: "image/tiff",
	}[req.Format]
	if mime == "" {
		mime = "image/jpeg"
	}

	// 刻意不在这里探测宽高与 DPI：服务端入库时会用 Pillow 读，
	// 这样 Go 侧不需要引入图像解码依赖，二进制保持小且无第三方包。
	return &scanner.Result{
		Data:    data,
		MIME:    mime,
		DPI:     req.DPI,
		Backend: b.Name(),
	}, nil
}
