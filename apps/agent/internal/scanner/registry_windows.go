//go:build windows

package scanner

import "github.com/zhihancn/postmarkTrip/apps/agent/internal/wia"

// NewBackend 在 Windows 上返回 WIA 后端。
//
// 为什么是 WIA 而不是 TWAIN：M1136 的随机软件同时支持 WIA 与 TWAIN
// （见 docs/need/02 D1 的实测参数），而 WIA 可以从 PowerShell 直接调用 COM，
// 不需要链接任何 SDK —— 这是「纯 Go + 命令行工具」路线能成立的关键。
func NewBackend() Backend { return wia.New() }
