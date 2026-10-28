//go:build linux

// Package sane 通过 SANE 的 scanimage 命令行驱动扫描仪。
//
// 为什么用 scanimage 而不是 python-sane：这个 Agent 的部署约束是
// "必须是单文件、零依赖、能在没有 Python 的机器上跑"（docs/ok/04 §3.2）。
// scanimage 由 sane-utils 提供，几乎所有发行版都有。
package sane

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"regexp"
	"strings"
	"time"

	"github.com/zhihancn/postmarkTrip/apps/agent/internal/scanner"
)

// deviceLine 匹配 scanimage -L 的输出行，例如：
//
//	device `airscan:e0:HP LaserJet MFP M1136' is a HP LaserJet MFP M1136 scanner
//
// 注意反引号在 Go 原始字符串里不能出现，所以这里用解释型字符串并转义。
var deviceLine = regexp.MustCompile("^device\\s+[`']([^`']+)[`']\\s+is\\s+a\\s+(.+?)(\\s+scanner)?$")

// Backend 是 SANE 后端。
type Backend struct {
	Timeout time.Duration
	// Scanimage 可覆盖（测试用）。
	Scanimage string
}

// New 返回一个 SANE 后端。
func New() *Backend { return &Backend{Timeout: 180 * time.Second} }

func (b *Backend) Name() string { return "sane" }

func (b *Backend) bin() (string, error) {
	if b.Scanimage != "" {
		return b.Scanimage, nil
	}
	p, err := exec.LookPath("scanimage")
	if err != nil {
		return "", &scanner.ToolNotFoundError{
			Tool:    "scanimage",
			Install: "请安装 sane-utils（Debian/Ubuntu: apt install sane-utils）",
		}
	}
	return p, nil
}

func (b *Backend) List(ctx context.Context) ([]scanner.Device, error) {
	bin, err := b.bin()
	if err != nil {
		return nil, err
	}
	cctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()

	var stdout, stderr bytes.Buffer
	cmd := exec.CommandContext(cctx, bin, "-L")
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	// scanimage -L 在"没有设备"时以非零码退出，这不算错误
	_ = cmd.Run()

	devices := []scanner.Device{}
	for _, line := range strings.Split(stdout.String(), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		m := deviceLine.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		devices = append(devices, scanner.Device{
			ID:   m[1],
			Name: strings.TrimSpace(m[2]),
			Capabilities: scanner.Capabilities{
				Sources: []scanner.Source{scanner.SourceFlatbed, scanner.SourceADF},
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
	bin, err := b.bin()
	if err != nil {
		return nil, err
	}

	mode := "Gray"
	if req.Color {
		mode = "Color"
	}
	format := map[scanner.Format]string{
		scanner.FormatJPEG: "jpeg",
		scanner.FormatPNG:  "png",
		scanner.FormatTIFF: "tiff",
	}[req.Format]
	if format == "" {
		format = "jpeg"
	}
	mime := map[scanner.Format]string{
		scanner.FormatJPEG: "image/jpeg",
		scanner.FormatPNG:  "image/png",
		scanner.FormatTIFF: "image/tiff",
	}[req.Format]
	if mime == "" {
		mime = "image/jpeg"
	}

	// 关键：必须是「无损、不增强」的原始采集。
	// 扫描仪固件的自动纠偏/去背/锐化会破坏油墨边缘信息（docs/ok/02 §1.3），
	// 所以这里显式关闭这些选项。
	args := []string{
		"--format=" + format,
		"--mode=" + mode,
		fmt.Sprintf("--resolution=%d", req.DPI),
	}
	if req.DeviceID != "" {
		args = append([]string{"-d", req.DeviceID}, args...)
	}
	if req.Source != "" {
		args = append(args, "--source="+string(req.Source))
	}

	cctx, cancel := context.WithTimeout(ctx, b.Timeout)
	defer cancel()

	var stdout, stderr bytes.Buffer
	cmd := exec.CommandContext(cctx, bin, args...)
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		return nil, fmt.Errorf("scanimage 失败：%s", msg)
	}
	if stdout.Len() == 0 {
		return nil, fmt.Errorf("scanimage 返回了空数据")
	}

	// 同 WIA 后端：宽高与 DPI 交给服务端的 Pillow 探测，Go 侧不引图像依赖。
	return &scanner.Result{
		Data:    stdout.Bytes(),
		MIME:    mime,
		DPI:     req.DPI,
		Backend: b.Name(),
	}, nil
}
