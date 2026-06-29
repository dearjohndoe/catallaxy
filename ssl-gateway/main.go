package main

import (
	"context"
	"embed"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path"
	"strconv"
	"strings"
	"time"

	"golang.org/x/crypto/acme/autocert"
)

// Landing site (catallaxy.cc) served on plain browser visits — i.e. requests
// with no X-Agent-Endpoint. Agent traffic always carries that header/param,
// so there is no path collision with the proxy.
//go:embed site
var siteFS embed.FS

var siteHandler = func() http.Handler {
	sub, err := fs.Sub(siteFS, "site")
	if err != nil {
		log.Fatalf("embed site: %v", err)
	}
	return http.FileServer(http.FS(sub))
}()

func main() {
	domain := os.Getenv("DOMAIN")
	listen := os.Getenv("LISTEN")

	mux := http.NewServeMux()
	mux.HandleFunc("/health", healthHandler)
	mux.HandleFunc("/img", imageProxyHandler)
	mux.HandleFunc("/", proxyHandler)
	handler := corsMiddleware(mux)

	if domain != "" {
		m := &autocert.Manager{
			Cache:      autocert.DirCache("certs"),
			Prompt:     autocert.AcceptTOS,
			HostPolicy: autocert.HostWhitelist(domain),
		}
		srv := &http.Server{
			Handler:   handler,
			TLSConfig: m.TLSConfig(),
		}
		go http.ListenAndServe(":80", m.HTTPHandler(nil))
		log.Printf("ssl-gateway listening on https://%s", domain)
		log.Fatal(srv.ListenAndServeTLS("", ""))
	} else {
		if listen == "" {
			listen = ":8080"
		}
		log.Printf("ssl-gateway listening on %s (no TLS)", listen)
		log.Fatal(http.ListenAndServe(listen, handler))
	}
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"status":"ok"}`))
}

// CORS headers set by the gateway — stripped from upstream responses to avoid duplication.
var corsHeaders = map[string]bool{
	"Access-Control-Allow-Origin":      true,
	"Access-Control-Allow-Methods":     true,
	"Access-Control-Allow-Headers":     true,
	"Access-Control-Allow-Credentials": true,
	"Access-Control-Expose-Headers":    true,
	"Access-Control-Max-Age":           true,
}

func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "*")
		w.Header().Set("Access-Control-Expose-Headers", "*")
		if r.Method == "OPTIONS" {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// safeDialer resolves DNS and checks ALL IPs against private ranges
// before establishing a connection — prevents DNS rebinding attacks.
var safeDialer = &net.Dialer{Timeout: 10 * time.Second}

func safeDialContext(ctx context.Context, network, addr string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, err
	}

	ips, err := net.DefaultResolver.LookupIPAddr(ctx, host)
	if err != nil {
		return nil, err
	}

	for _, ip := range ips {
		if ip.IP.IsLoopback() || ip.IP.IsPrivate() || ip.IP.IsLinkLocalUnicast() || ip.IP.IsUnspecified() {
			return nil, fmt.Errorf("private address %s not allowed", ip.IP)
		}
	}

	// Connect to the first resolved IP explicitly (so the runtime can't pick a different one)
	return safeDialer.DialContext(ctx, network, net.JoinHostPort(ips[0].IP.String(), port))
}

func getTimeout() time.Duration {
	if s := os.Getenv("TIMEOUT"); s != "" {
		if sec, err := strconv.Atoi(s); err == nil {
			return time.Duration(sec) * time.Second
		}
	}
	return 120 * time.Second
}

var transport = &http.Transport{
	DialContext:           safeDialContext,
	ResponseHeaderTimeout: getTimeout(),
	MaxIdleConns:          100,
	IdleConnTimeout:       90 * time.Second,
}

func proxyHandler(w http.ResponseWriter, r *http.Request) {
	endpoint := r.Header.Get("X-Agent-Endpoint")
	if endpoint == "" {
		endpoint = r.URL.Query().Get("endpoint")
	}
	if endpoint == "" {
		// No agent endpoint → normal browser visit: serve the landing site.
		siteHandler.ServeHTTP(w, r)
		return
	}

	targetURL, err := url.Parse(endpoint)
	if err != nil || targetURL.Host == "" {
		http.Error(w, `{"error":"invalid endpoint URL"}`, http.StatusBadRequest)
		return
	}

	// Build final path: endpoint base + request path
	targetURL.Path = joinPath(targetURL.Path, r.URL.Path)
	q := r.URL.Query()
	q.Del("endpoint")
	targetURL.RawQuery = q.Encode()

	proxy := &httputil.ReverseProxy{
		Director: func(req *http.Request) {
			req.URL = targetURL
			req.Host = targetURL.Host

			// Remove gateway-specific header
			req.Header.Del("X-Agent-Endpoint")
		},
		Transport: transport,
		ModifyResponse: func(resp *http.Response) error {
			// Strip upstream CORS headers — gateway sets its own
			for h := range corsHeaders {
				resp.Header.Del(h)
			}
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			log.Printf("upstream error: %s %s -> %v", r.Method, targetURL.String(), err)
			http.Error(w, `{"error":"upstream unreachable"}`, http.StatusBadGateway)
		},
	}

	proxy.ServeHTTP(w, r)
}

// Image proxy — lets https-hosted frontends load images from http agent endpoints
// without mixed-content blocking. Locked down to image MIME types only; cookies
// and credentials are stripped both ways to prevent SSRF-style session leaks.

const imageMaxBytes = 5 * 1024 * 1024

var allowedImageTypes = map[string]bool{
	"image/png":  true,
	"image/jpeg": true,
	"image/gif":  true,
	"image/webp": true,
}

func imageProxyHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		http.Error(w, `{"error":"method not allowed"}`, http.StatusMethodNotAllowed)
		return
	}

	raw := r.URL.Query().Get("url")
	if raw == "" {
		http.Error(w, `{"error":"missing url"}`, http.StatusBadRequest)
		return
	}
	if len(raw) > 2048 {
		http.Error(w, `{"error":"url too long"}`, http.StatusBadRequest)
		return
	}

	target, err := url.Parse(raw)
	if err != nil || target.Host == "" {
		http.Error(w, `{"error":"invalid url"}`, http.StatusBadRequest)
		return
	}
	if target.Scheme != "http" && target.Scheme != "https" {
		http.Error(w, `{"error":"scheme not allowed"}`, http.StatusBadRequest)
		return
	}
	// Block SVG by path extension too — response content-type is also checked
	// below, but extension gives an early out and defends against servers that
	// mislabel content.
	ext := strings.ToLower(path.Ext(target.Path))
	if ext == ".svg" || ext == ".svgz" {
		http.Error(w, `{"error":"svg not allowed"}`, http.StatusUnsupportedMediaType)
		return
	}

	req, err := http.NewRequestWithContext(r.Context(), r.Method, target.String(), nil)
	if err != nil {
		http.Error(w, `{"error":"bad request"}`, http.StatusBadRequest)
		return
	}
	req.Header.Set("Accept", "image/*")
	req.Header.Set("User-Agent", "catallaxy-gateway/1.0")

	resp, err := imageClient.Do(req)
	if err != nil {
		log.Printf("image upstream error: %s -> %v", target.String(), err)
		http.Error(w, `{"error":"upstream unreachable"}`, http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		http.Error(w, `{"error":"upstream error"}`, http.StatusBadGateway)
		return
	}

	ct := strings.ToLower(strings.TrimSpace(strings.Split(resp.Header.Get("Content-Type"), ";")[0]))
	if !allowedImageTypes[ct] {
		http.Error(w, `{"error":"unsupported content-type"}`, http.StatusUnsupportedMediaType)
		return
	}

	if cl := resp.ContentLength; cl > imageMaxBytes {
		http.Error(w, `{"error":"image too large"}`, http.StatusRequestEntityTooLarge)
		return
	}

	w.Header().Set("Content-Type", ct)
	w.Header().Set("Cache-Control", "public, max-age=86400")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if resp.ContentLength > 0 {
		w.Header().Set("Content-Length", strconv.FormatInt(resp.ContentLength, 10))
	}
	w.WriteHeader(http.StatusOK)

	if r.Method == http.MethodHead {
		return
	}

	written, err := io.Copy(w, io.LimitReader(resp.Body, imageMaxBytes+1))
	if err != nil {
		log.Printf("image copy error: %s -> %v", target.String(), err)
		return
	}
	if written > imageMaxBytes {
		log.Printf("image exceeded cap: %s (%d bytes)", target.String(), written)
	}
}

var imageClient = &http.Client{
	Timeout:   30 * time.Second,
	Transport: transport,
	CheckRedirect: func(req *http.Request, via []*http.Request) error {
		if len(via) >= 3 {
			return fmt.Errorf("too many redirects")
		}
		if req.URL.Scheme != "http" && req.URL.Scheme != "https" {
			return fmt.Errorf("redirect to non-http scheme blocked")
		}
		return nil
	},
}

func joinPath(base, extra string) string {
	a := strings.HasSuffix(base, "/")
	b := strings.HasPrefix(extra, "/")
	switch {
	case a && b:
		return base + extra[1:]
	case !a && !b:
		return base + "/" + extra
	}
	return base + extra
}
