//go:build linux

package scanner

import "github.com/zhihancn/postmarkTrip/apps/agent/internal/sane"

// NewBackend 在 Linux 上返回 SANE 后端（经由 scanimage）。
//
// 注意：用户的 M1136 是 USB-only 且不支持 eSCL/AirScan，
// 所以 sane-airscan（网络扫描）这条首选路线在他的设备上用不了
// （见 docs/ok/04 §1.2）。这里保留 SANE 是为了支持将来换成网络扫描仪，
// 或把 USB 线插到一台原生 Linux 机器的场景（docs/need/04 §4.4 的 S4）。
func NewBackend() Backend { return sane.New() }
