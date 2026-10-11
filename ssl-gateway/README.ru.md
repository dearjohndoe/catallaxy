# Catallaxy — SSL Gateway

> [English version](README.md)

HTTPS-прокси для агентов без своего сертификата. Браузер на HTTPS-странице не может обратиться к агенту по `http://`; шлюз принимает запрос по HTTPS и передаёт его агенту. Сертификат Let's Encrypt выпускается и продлевается сам.

- **Куда проксировать.** Агента задаёт заголовок `X-Agent-Endpoint` или параметр `?endpoint=`. Шлюз добавляет CORS-заголовки.
- **`GET /img?url=<http-image-url>`.** Прокси для картинок с http-агентов. Только PNG/JPEG/GIF/WebP (без SVG), до 5 МБ. Cookies и `Authorization` вырезаются, приватные сети заблокированы.
- **`GET /health`.** Проверка живости.
- **Лендинг.** На запрос без адреса агента отдаётся лендинг из `site/`.

## Запуск

A-запись домена должна указывать на сервер.

```bash
docker build -t ssl-gateway .
docker run -d --name ssl-gw --restart always \
  -p 80:80 -p 443:443 -v $(pwd)/certs:/certs \
  -e DOMAIN=api.yourdomain.com ssl-gateway
docker logs -f ssl-gw
```

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `DOMAIN` | — | домен для сертификата; без него шлюз работает по обычному HTTP |
| `LISTEN` | `:8080` | адрес без TLS (когда `DOMAIN` пуст) |
| `TIMEOUT` | `120` | таймаут ответа агента, в секундах |

Фронт подключает шлюз через `VITE_SSL_GATEWAY`.
