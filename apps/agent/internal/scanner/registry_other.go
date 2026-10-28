//go:build !windows && !linux

package scanner

import "context"

// NewBackend 在其他平台（macOS 等）上返回一个明确报错的后端。
//
// 刻意不静默降级：让用户看到"这个平台没实现"，而不是"扫描失败"这种含糊信息。
// macOS 上如果将来要做，正确路线是 Image Capture 的 ICA 接口
// （命令行可用 `scanimage` 的 macOS 移植或 `imagesnap`，但那是另一件事）。
func NewBackend() Backend { return unsupportedBackend{} }

type unsupportedBackend struct{}

func (unsupportedBackend) Name() string { return "unsupported" }

func (unsupportedBackend) List(context.Context) ([]Device, error) {
	return nil, ErrUnsupportedPlatform
}

func (unsupportedBackend) Scan(context.Context, ScanRequest) (*Result, error) {
	return nil, ErrUnsupportedPlatform
}
