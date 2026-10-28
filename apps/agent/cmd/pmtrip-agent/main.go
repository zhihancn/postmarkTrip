// Command pmtrip-agent 是跑戳日志的本地扫描 Agent。
//
// 它必须跑在「USB 线插着扫描仪的那台机器」上 —— 因为 USB 扫描仪没有 IP 地址，
// 任何跑在别的机器上的服务都不可能通过网络访问它（docs/need/04 §四）。
//
// 用法：
//
//	pmtrip-agent --origin http://192.168.1.10:9999
//
// 默认只监听 127.0.0.1:18080，不对局域网暴露。
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/zhihancn/postmarkTrip/apps/agent/internal/httpapi"
	"github.com/zhihancn/postmarkTrip/apps/agent/internal/scanner"
)

// version 由构建时通过 -ldflags 注入。
var version = "0.1.0-dev"

func main() {
	var (
		addr     = flag.String("addr", "127.0.0.1:18080", "监听地址（默认仅回环）")
		origins  = flag.String("origin", "", "允许的网页来源，逗号分隔，例如 http://192.168.1.10:9999")
		token    = flag.String("token", "", "可选口令（仅在需要暴露到局域网时使用）")
		listOnly = flag.Bool("list", false, "只列出设备然后退出（用于排查）")
		verbose  = flag.Bool("v", false, "输出调试日志")
	)
	flag.Parse()

	level := slog.LevelInfo
	if *verbose {
		level = slog.LevelDebug
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: level}))

	backend := scanner.NewBackend()

	if *listOnly {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		devices, err := backend.List(ctx)
		if err != nil {
			log.Error("枚举设备失败", "err", err)
			var nf *scanner.ToolNotFoundError
			if errors.As(err, &nf) {
				fmt.Fprintln(os.Stderr, "\n提示："+nf.Install)
			}
			os.Exit(1)
		}
		if len(devices) == 0 {
			fmt.Println("未找到扫描设备。")
			fmt.Println("  Windows: 确认设备已开机、USB 已连接，且驱动已安装（WIA 可用）")
			fmt.Println("  Linux  : 运行 scanimage -L 看是否列出设备")
			return
		}
		fmt.Printf("后端：%s，共 %d 台设备\n", backend.Name(), len(devices))
		for _, d := range devices {
			fmt.Printf("  - %s\n      id: %s\n", d.Name, d.ID)
		}
		return
	}

	allow := []string{}
	for _, o := range strings.Split(*origins, ",") {
		if o = strings.TrimSpace(o); o != "" {
			allow = append(allow, o)
		}
	}
	if len(allow) == 0 {
		log.Warn("未指定 --origin，浏览器将无法读取扫描结果。" +
			"请传入服务器的访问地址，例如 --origin http://192.168.1.10:9999")
	}
	if !isLoopback(*addr) {
		log.Warn("监听地址不是回环地址 —— 局域网内任何人都能触发扫描",
			"addr", *addr, "mitigation", "请设置 --token")
	}

	srv := &http.Server{
		Addr: *addr,
		Handler: httpapi.New(httpapi.Config{
			Addr:         *addr,
			AllowOrigins: allow,
			Token:        *token,
			Version:      version,
		}, backend, log).Handler(),
		ReadHeaderTimeout: 10 * time.Second,
		// 扫描可能很慢（600dpi 彩色大图），写超时给宽一些
		WriteTimeout: 5 * time.Minute,
		IdleTimeout:  2 * time.Minute,
	}

	go func() {
		log.Info("扫描 Agent 启动", "addr", *addr, "backend", backend.Name(),
			"version", version, "origins", allow)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("服务异常退出", "err", err)
			os.Exit(1)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop

	log.Info("正在停止…")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Warn("停止时出错", "err", err)
	}
}

func isLoopback(addr string) bool {
	host := addr
	if i := strings.LastIndex(addr, ":"); i >= 0 {
		host = addr[:i]
	}
	host = strings.Trim(host, "[]")
	return host == "127.0.0.1" || host == "localhost" || host == "::1"
}
