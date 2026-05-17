"""list-seller — продаёт строки из захардкоженного списка по одной за вызов.

Контракт sidecar:
- describe → {"args_schema": ..., "result_schema": ...}
- invoke success → stdout: {"result": {"type": "string", "data": "<token>"}}
- inventory empty → stdout: {"error": "out_of_stock", "reason": "..."} (exit 0)
- любая другая ошибка → exit != 0, sidecar вернёт refund

Окружение:
- TOKENS_FILE_PATH — путь до файла с токенами; в имени допустим плейсхолдер
  <sku>, который агент подставляет из body.sku (default — sku "default").
  Пример: /var/lib/list-seller/tokens-<sku>.txt
- LOG_FILE_PATH — путь до лога продаж (опц., по умолчанию рядом с agent.py).
- CALLER_ADDRESS, CALLER_TX_HASH, PAYMENT_RAIL — проставляются sidecar'ом.

Конкурентная безопасность:
- Эксклюзивный fcntl.flock на отдельном lock-файле (`<tokens>.lock`).
  Лок-файл никогда не переименовывается, поэтому семантика flock сохраняется
  при atomic rename файла токенов.
- Токены переписываются через tmp + os.replace + fsync директории — никаких
  частично записанных состояний при падении.
- Порядок: сначала коммитим списание (rename + fsync), пишем лог, и только
  потом печатаем результат. Если процесс падает после коммита, но до stdout —
  sidecar выдаст refund (а юнит уже потерян), но повторной выдачи не будет.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import sys
import time
from pathlib import Path

ARGS_SCHEMA: dict = {}
RESULT_SCHEMA = {"type": "string"}


def _mask(token: str, n: int = 4) -> str:
    if len(token) <= 2 * n + 3:
        return "***"
    return f"{token[:n]}...{token[-n:]}"


def _resolve_tokens_path(sku: str) -> Path:
    template = os.environ.get("TOKENS_FILE_PATH")
    if not template:
        raise RuntimeError("TOKENS_FILE_PATH env var is not set")
    return Path(template.replace("<sku>", sku))


def _resolve_log_path() -> Path:
    custom = os.environ.get("LOG_FILE_PATH")
    if custom:
        return Path(custom)
    return Path(__file__).resolve().parent / "log.txt"


def _fsync_dir(path: Path) -> None:
    try:
        dir_fd = os.open(str(path), os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _pop_first_token(tokens_path: Path) -> str | None:
    """Достаёт первую строку из tokens_path под эксклюзивным локом.
    Возвращает токен или None, если список пуст. Падает, если файла нет.
    """
    if not tokens_path.exists():
        raise FileNotFoundError(f"tokens file not found: {tokens_path}")

    tokens_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = tokens_path.with_name(tokens_path.name + ".lock")

    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        try:
            content = tokens_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

        lines = [ln for ln in content.splitlines() if ln.strip()]
        if not lines:
            return None

        token = lines[0]
        remaining = lines[1:]

        tmp_path = tokens_path.with_name(tokens_path.name + ".tmp")
        # O_TRUNC чтобы не подобрать чужой хвост, если tmp остался от падения.
        with os.fdopen(
            os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
            "w",
            encoding="utf-8",
        ) as tmpf:
            if remaining:
                tmpf.write("\n".join(remaining) + "\n")
            tmpf.flush()
            os.fsync(tmpf.fileno())

        os.replace(tmp_path, tokens_path)
        _fsync_dir(tokens_path.parent)

        return token
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _append_log(log_path: Path, entry: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_lock_path = log_path.with_name(log_path.name + ".lock")
    lock_fd = os.open(log_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        with os.fdopen(
            os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600),
            "a",
            encoding="utf-8",
        ) as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def process_task(task: dict) -> dict:
    started_ns = time.monotonic_ns()
    body = task.get("body") or {}
    sku = (body.get("sku") or task.get("sku") or "default").strip() or "default"

    tokens_path = _resolve_tokens_path(sku)

    token = _pop_first_token(tokens_path)
    if token is None:
        return {
            "error": "out_of_stock",
            "reason": f"no tokens left for sku={sku}",
            "sku": sku,
        }

    # Списание уже зафиксировано на диске. Логируем и возвращаем результат.
    now = time.time()
    log_entry = {
        "ts": int(now),
        "ts_iso": _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc).isoformat(),
        "sku": sku,
        "capability": task.get("capability", ""),
        "mode": task.get("mode") or "invoke",
        "caller_address": os.environ.get("CALLER_ADDRESS", ""),
        "caller_tx_hash": os.environ.get("CALLER_TX_HASH", ""),
        "payment_rail": os.environ.get("PAYMENT_RAIL", ""),
        "token_preview": _mask(token),
        "duration_ms": (time.monotonic_ns() - started_ns) // 1_000_000,
        "body": body,
    }
    try:
        _append_log(_resolve_log_path(), log_entry)
    except Exception as exc:
        # Лог — не критичный путь. На stderr — видно sidecar'у, но не валим
        # сделку: токен уже списан, пользователь должен получить его.
        print(f"warning: failed to write log: {exc}", file=sys.stderr)

    return {"result": {"type": "string", "data": _format_delivery(token, sku, log_entry)}}


def _format_delivery(token: str, sku: str, log_entry: dict) -> str:
    """Финальная строка для клиента: ключ + метаданные сделки + напоминания."""
    lines = [
        "# 🎁 Ваш код",
        "",
        f"`{token}`",
        "",
        f"Или перейди по ссылке с десктопа: https://claude.ai/gift/redeem?code={token}",
        "",
        "## 📋 Данные сделки — **сохраните себе**",
        "",
        f"- **SKU:** `{sku}`",
        f"- **Время:** {log_entry.get('ts_iso', '')}",
        f"- **TX hash:** `{log_entry.get('caller_tx_hash', '')}`",
        f"- **Кошелёк:** `{log_entry.get('caller_address', '')}`",
        f"- **Рельс оплаты:** {log_entry.get('payment_rail', '')}",
        "",
        "## ✅ Как использовать",
        "",
        "1. Откройте `https://claude.ai/gift/redeem` в **браузере на десктопе**.",
        "2. Войдите в Claude-аккаунт.",
        "3. Вставьте код выше, подтвердите.",
        "",
        "Срок действия — **365 дней** с даты выпуска. Подписка стартует с момента редемпшна.",
        "",
        "## 🛡 Откуда код",
        "",
        "Код куплен напрямую у Anthropic по официальной цене с легитимной карты. Не carding, не stolen CC, не promo-абуз — отзыва через chargeback быть не может, активация на ваш аккаунт **навсегда ваша**.",
        "",
        "## 🆘 Если что-то пошло не так",
        "",
        "Сохраните этот ответ целиком (особенно `TX hash`) и пишите в [@catallaxy_support_bot](https://t.me/catallaxy_support_bot). Без `TX hash` мы не сможем найти вашу покупку.",
    ]
    return "\n".join(lines)


def main() -> None:
    task = json.load(sys.stdin)
    mode = task.get("mode")

    if mode == "describe":
        print(json.dumps({"args_schema": ARGS_SCHEMA, "result_schema": RESULT_SCHEMA}))
        return

    result = process_task(task)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
