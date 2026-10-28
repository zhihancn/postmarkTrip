// Package scanner 定义扫描后端的中立抽象。
//
// 设计要点：**全部后端都是纯 Go，不引入 cgo。**
// 平台能力（Windows WIA / Linux SANE）通过调用系统自带的命令行工具实现，
// 而不是链接厂商 SDK。这样做的代价是多一次进程启动（几十毫秒），
// 换来的是：
//   - 单一静态二进制，可直接在用户的 Windows 机器上双击运行（不需要 Python/Node/VC++ 运行库）
//   - 交叉编译简单，不需要为三个平台各配一套构建环境
//   - 与 docs/ok/04 §3.4「不引入 cgo 与 Rust 以控制运维复杂度」的判断一致
package scanner

import (
	"context"
	"errors"
	"fmt"
)

// Source 是进纸方式。
type Source string

const (
	SourceFlatbed   Source = "flatbed"
	SourceADF       Source = "adf"
	SourceADFDuplex Source = "adf_duplex"
)

// Format 是图像容器格式。
type Format string

const (
	FormatJPEG Format = "jpeg"
	FormatPNG  Format = "png"
	FormatTIFF Format = "tiff"
)

// Capabilities 描述一台设备的能力。前端的选项列表由它生成 ——
// 不要在前端硬编码分辨率与色彩模式。
type Capabilities struct {
	Sources  []Source `json:"sources"`
	MaxDPIX  int      `json:"max_dpi_x"`
	MaxDPIY  int      `json:"max_dpi_y"`
	Color    bool     `json:"color"`
	Formats  []Format `json:"formats"`
	ADF      bool     `json:"adf"`
	Duplex   bool     `json:"duplex"`
	MaxPages int      `json:"max_pages,omitempty"`
}

// Device 是一台可用的扫描设备。
type Device struct {
	ID           string       `json:"id"`
	Name         string       `json:"name"`
	Capabilities Capabilities `json:"capabilities"`
}

// ScanRequest 是一次扫描请求。
type ScanRequest struct {
	DeviceID string `json:"device_id"`
	DPI      int    `json:"dpi"`
	Color    bool   `json:"color"`
	Source   Source `json:"source"`
	Format   Format `json:"format"`
}

// DefaultDPI 是用户的实际选择。
//
// 依据 docs/need/03 §三.6 的实测：日戳外圈小字 5mm、风景戳 3mm，
// 600 dpi 下最小字高约 71 px，已足够 OCR；1200 dpi 只有边际收益且慢得多。
const DefaultDPI = 600

// Normalize 补全缺省值并做基本校验。
func (r *ScanRequest) Normalize() error {
	if r.DPI == 0 {
		r.DPI = DefaultDPI
	}
	if r.DPI < 75 || r.DPI > 4800 {
		return fmt.Errorf("分辨率 %d 超出合理范围（75–4800）", r.DPI)
	}
	if r.Source == "" {
		r.Source = SourceFlatbed
	}
	if r.Format == "" {
		r.Format = FormatJPEG
	}
	return nil
}

// Result 是一次扫描的产物。
type Result struct {
	Data    []byte
	MIME    string
	Width   int
	Height  int
	DPI     int
	Backend string
}

// Backend 是平台后端。
type Backend interface {
	// Name 用于诊断输出，例如 "wia" / "sane"。
	Name() string
	// List 返回可用设备。没有设备时返回空切片与 nil error。
	List(ctx context.Context) ([]Device, error)
	// Scan 执行一次扫描。多页由调用方循环，本接口只负责一页。
	Scan(ctx context.Context, req ScanRequest) (*Result, error)
}

// ErrNoDevice 表示系统上没有可用设备。
var ErrNoDevice = errors.New("未找到可用的扫描设备")

// ErrUnsupportedPlatform 表示当前平台没有实现后端。
var ErrUnsupportedPlatform = errors.New("当前平台不支持扫描")

// ToolNotFoundError 表示所依赖的系统命令不存在。
type ToolNotFoundError struct {
	Tool    string
	Install string // 给用户看的安装提示
}

func (e *ToolNotFoundError) Error() string {
	return fmt.Sprintf("找不到命令 %q；%s", e.Tool, e.Install)
}
