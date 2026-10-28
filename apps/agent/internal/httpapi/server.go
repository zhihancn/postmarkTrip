// Package httpapi 暴露扫描 Agent 的本地 HTTP 接口。
//
// 拓扑依据 docs/need/04 §4.5（已与用户确认）：
//
//	浏览器(Windows) --① 点"扫描"--> http://localhost:18080
//	                                Agent 用 WIA 驱动 M1136 取图
//	        <--② 图像字节直接返回--
//	浏览器 --③ 用常规的 /api/assets 上传--> Linux 服务器（分割 / 识别 / 入库）
//
// **刻意让 Agent 直接返回图像字节，而不是自己上传到服务器。** 理由：
//   - 浏览器已经是登录态，复用「手工上传」的同一套 API —— 服务端不需要新增端点
//   - Agent 因此**不持有任何凭据**，它只是一个"本机外设驱动"
//   - 少一条认证路径就少一处可能出错的地方
package httpapi

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/zhihancn/postmarkTrip/apps/agent/internal/scanner"
)

// Config 是 Agent 的运行配置。
type Config struct {
	// Addr 默认必须是回环地址。绑定到非回环意味着局域网内任何人都能触发扫描，
	// 甚至读取扫描结果 —— 这是明确不允许的默认行为。
	Addr string
	// AllowOrigins 是允许读取响应的来源白名单。
	//
	// ★ 这是本 Agent 的**安全边界**。它只绑 127.0.0.1，所以只有本机页面能连上；
	// 而 CORS 白名单决定哪个本机页面能**读到**扫描结果。
	// 不设白名单（或设为 *）等于让任意网页都能驱动扫描仪并取走图像。
	AllowOrigins []string
	// Token 是可选的额外口令。默认空 —— 回环 + CORS 已经足够。
	// 只有在用户坚持把 Agent 暴露到局域网时才需要它。
	Token string
	// Version 仅用于诊断输出。
	Version string
}

// Server 是 Agent 的 HTTP 服务。
type Server struct {
	cfg     Config
	backend scanner.Backend
	log     *slog.Logger
}

// New 构造一个 Server。
func New(cfg Config, backend scanner.Backend, log *slog.Logger) *Server {
	if log == nil {
		log = slog.Default()
	}
	return &Server{cfg: cfg, backend: backend, log: log}
}

// Handler 返回完整的路由（含中间件）。
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("GET /devices", s.handleDevices)
	mux.HandleFunc("POST /scan", s.handleScan)

	return s.withCORS(s.withToken(mux))
}

// --------------------------------------------------------------------------- //
//  中间件
// --------------------------------------------------------------------------- //

func (s *Server) originAllowed(origin string) bool {
	if origin == "" {
		// 同源请求（例如用 curl 直接打）不带 Origin，放行
		return true
	}
	for _, a := range s.cfg.AllowOrigins {
		if a == "*" || strings.EqualFold(a, origin) {
			return true
		}
	}
	return false
}

func (s *Server) withCORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		if origin != "" && s.originAllowed(origin) {
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Set("Vary", "Origin")
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
			w.Header().Set("Access-Control-Allow-Headers", "Content-Type, X-Agent-Token")
			w.Header().Set("Access-Control-Max-Age", "600")
		}

		if r.Method == http.MethodOptions {
			if origin != "" && !s.originAllowed(origin) {
				// 预检不通过：不回 CORS 头，浏览器自会拦截
				w.WriteHeader(http.StatusForbidden)
				return
			}
			w.WriteHeader(http.StatusNoContent)
			return
		}

		// 非白名单来源的**简单请求**也要挡住 —— 否则一个 <img src> 之类的
		// 侧面通道仍可能触发扫描。
		if origin != "" && !s.originAllowed(origin) {
			s.log.Warn("拒绝非白名单来源", "origin", origin, "path", r.URL.Path)
			http.Error(w, "origin not allowed", http.StatusForbidden)
			return
		}

		next.ServeHTTP(w, r)
	})
}

func (s *Server) withToken(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if s.cfg.Token == "" || r.URL.Path == "/health" {
			next.ServeHTTP(w, r)
			return
		}
		if r.Header.Get("X-Agent-Token") != s.cfg.Token {
			http.Error(w, "invalid agent token", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// --------------------------------------------------------------------------- //
//  处理器
// --------------------------------------------------------------------------- //

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := contextWithTimeout(r, 15*time.Second)
	defer cancel()

	devices, err := s.backend.List(ctx)
	resp := map[string]any{
		"ok":      err == nil,
		"backend": s.backend.Name(),
		"version": s.cfg.Version,
		"devices": len(devices),
	}
	if err != nil {
		resp["ok"] = false
		resp["error"] = err.Error()
		var nf *scanner.ToolNotFoundError
		if errors.As(err, &nf) {
			resp["hint"] = nf.Install
		}
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleDevices(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := contextWithTimeout(r, 30*time.Second)
	defer cancel()

	devices, err := s.backend.List(ctx)
	if err != nil {
		var nf *scanner.ToolNotFoundError
		if errors.As(err, &nf) {
			writeJSON(w, http.StatusServiceUnavailable, map[string]any{
				"error": err.Error(), "hint": nf.Install,
			})
			return
		}
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"devices": devices})
}

func (s *Server) handleScan(w http.ResponseWriter, r *http.Request) {
	var req scanner.ScanRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "请求体不是合法的扫描参数"})
		return
	}
	if err := req.Normalize(); err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"error": err.Error()})
		return
	}

	s.log.Info("开始扫描", "device", req.DeviceID, "dpi", req.DPI,
		"color", req.Color, "source", req.Source, "format", req.Format)

	res, err := s.backend.Scan(r.Context(), req)
	if err != nil {
		s.log.Error("扫描失败", "err", err)
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}

	s.log.Info("扫描完成", "bytes", len(res.Data), "backend", res.Backend)

	// 直接返回图像字节。浏览器拿到后会走常规的 /api/assets 上传 ——
	// 与"手工上传"完全同一条路径，Agent 不参与认证。
	w.Header().Set("Content-Type", res.MIME)
	w.Header().Set("Content-Length", strconv.Itoa(len(res.Data)))
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Scan-Backend", res.Backend)
	w.Header().Set("X-Scan-DPI", strconv.Itoa(res.DPI))
	w.WriteHeader(http.StatusOK)
	if _, err := w.Write(res.Data); err != nil {
		s.log.Warn("写响应失败（客户端可能已断开）", "err", err)
	}
}
