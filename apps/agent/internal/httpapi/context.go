package httpapi

import (
	"context"
	"net/http"
	"time"
)

// contextWithTimeout 在请求自身的 context 之上再叠一个超时，取两者中更早到期的。
func contextWithTimeout(r *http.Request, d time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(r.Context(), d)
}
