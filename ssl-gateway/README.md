# Catallaxy — SSL Gateway

> [Русская версия](README.ru.md)

An HTTPS proxy for agents that have no certificate of their own. A browser on an HTTPS page cannot call an `http://` agent; the gateway takes the request over HTTPS and forwards it to the agent. The Let's Encrypt certificate is issued and renewed automatically.

- **Target.** The agent is set by the `X-Agent-Endpoint` header or the `?endpoint=` parameter. The proxy adds CORS headers.
- **`GET /img?url=<http-image-url>`.** Proxies images from http agents. PNG/JPEG/GIF/WebP only (no SVG), up to 5 MB. Cookies and `Authorization` are stripped. Private networks are blocked.
- **`GET /health`.** Health check.
- **Landing.** A request without an agent target gets the landing site from `site/`.

## Run

The domain's A record must point to the server.

```bash
docker build -t ssl-gateway .
docker run -d --name ssl-gw --restart always \
  -p 80:80 -p 443:443 -v $(pwd)/certs:/certs \
  -e DOMAIN=api.yourdomain.com ssl-gateway
docker logs -f ssl-gw
```

| Variable | Default | Purpose |
|---|---|---|
| `DOMAIN` | — | domain for the certificate; without it the gateway runs over plain HTTP |
| `LISTEN` | `:8080` | address without TLS (when `DOMAIN` is empty) |
| `TIMEOUT` | `120` | agent response timeout, seconds |

The frontend uses the gateway via `VITE_SSL_GATEWAY`.
