"""标准库 HTTP JSON 接口：同意签署/撤回、字段读取、留档、导出与家长授权去向。"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .api import ConsentBackend
from .domain import Purpose, Role
from .errors import ConcurrencyConflict, ConsentError, NotFound, PermissionDenied


def jsonable(value):
    """把领域对象递归转成可 JSON 序列化的结构。"""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {jsonable(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(jsonable(v) for v in value)
    return value


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _role(value: str) -> Role:
    return Role(value)


def _purpose(value: str) -> Purpose:
    return Purpose(value)


def make_server(
    backend: ConsentBackend, host: str = "127.0.0.1", port: int = 8080
) -> ThreadingHTTPServer:
    """构建 HTTP 服务；port=0 时由系统分配端口，便于测试。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "ConsentBackend/1.0"

        def log_message(self, *args) -> None:  # 静默访问日志
            pass

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        # ---- 基础工具 ----

        def _send(self, code: int, payload) -> None:
            body = json.dumps(jsonable(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _query(self) -> dict[str, str]:
            return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

        # ---- 路由 ----

        def _dispatch(self, method: str) -> None:
            segments = [s for s in urlparse(self.path).path.split("/") if s]
            try:
                payload, code = self._route(method, segments)
            except PermissionDenied as exc:
                payload, code = {"error": "permission_denied", "message": str(exc)}, 403
            except NotFound as exc:
                payload, code = {"error": "not_found", "message": str(exc)}, 404
            except ConcurrencyConflict as exc:
                payload, code = {"error": "concurrency_conflict", "message": str(exc)}, 409
            except (ConsentError, ValueError, KeyError) as exc:
                payload, code = {"error": "bad_request", "message": str(exc)}, 400
            self._send(code, payload)

        def _route(self, method: str, segments: list[str]):
            body = self._body() if method == "POST" else {}
            query = self._query()

            if method == "POST" and segments == ["v1", "minors"]:
                record = backend.register_minor(
                    body["minor_id"], body.get("display_name", ""), body.get("data")
                )
                return {"minor_id": record.minor_id}, 201

            if method == "POST" and len(segments) == 3 and segments[:2] == ["v1", "minors"] and segments[2] == "data":
                record = backend.data.update_fields(
                    body["actor_id"], _role(body["role"]), body["minor_id"], body["updates"]
                )
                return {"minor_id": record.minor_id}, 200

            if method == "POST" and segments == ["v1", "guardianship:establish"]:
                record = backend.guardianships.establish(
                    body["minor_id"], body["guardian_id"],
                    actor_id=body.get("actor_id", "api"), now=_dt(body.get("now")),
                )
                return record, 201

            if method == "POST" and segments == ["v1", "guardianship:change"]:
                record = backend.guardianships.change(
                    body["minor_id"], body["new_guardian_id"],
                    actor_id=body.get("actor_id", "api"), now=_dt(body.get("now")),
                )
                return record, 200

            if method == "POST" and segments == ["v1", "assignments"]:
                record = backend.add_assignment(
                    staff_id=body["staff_id"], role=_role(body["role"]),
                    minor_id=body["minor_id"], service_id=body["service_id"],
                    purposes=[_purpose(p) for p in body["purposes"]],
                    start=_dt(body.get("start")), end=_dt(body.get("end")),
                )
                return record, 201

            if method == "POST" and segments == ["v1", "consents:grant"]:
                record = backend.consents.grant(
                    body["minor_id"], _purpose(body["purpose"]), body["guardian_id"],
                    body["form_version"],
                    valid_from=_dt(body.get("valid_from")),
                    valid_until=_dt(body.get("valid_until")),
                    expected_head_seq=body.get("expected_head_seq"),
                    now=_dt(body.get("now")),
                )
                return record, 201

            if method == "POST" and segments == ["v1", "consents:withdraw"]:
                record = backend.consents.withdraw(
                    body["minor_id"], _purpose(body["purpose"]), body["guardian_id"],
                    expected_head_seq=body.get("expected_head_seq"),
                    now=_dt(body.get("now")),
                )
                return record, 200

            if method == "POST" and segments == ["v1", "archives"]:
                record = backend.archives.create_archive(
                    body["actor_id"], _role(body["role"]),
                    body["minor_id"], body["service_id"], now=_dt(body.get("now")),
                )
                return record, 201

            if method == "POST" and segments == ["v1", "exports"]:
                payload = backend.exports.request_export(
                    body["requester_id"], _role(body["role"]), body["minor_id"],
                    now=_dt(body.get("now")),
                )
                return payload, 200

            if method == "GET" and len(segments) == 4 and segments[:2] == ["v1", "minors"] and segments[3] == "fields":
                result = backend.access.read_fields(
                    query["actor_id"], _role(query["role"]), segments[2],
                    _purpose(query["purpose"]),
                    requested_fields=query["fields"].split(",") if query.get("fields") else None,
                )
                return result, 200

            if method == "GET" and len(segments) == 4 and segments[:2] == ["v1", "minors"] and segments[3] == "disclosure":
                view = backend.portal.disclosure_view(query["guardian_id"], segments[2])
                return view, 200

            if method == "GET" and len(segments) == 3 and segments[:2] == ["v1", "archives"]:
                snapshot = backend.archives.read_archive(
                    query["actor_id"], _role(query["role"]), segments[2],
                    _purpose(query["purpose"]),
                    justification=query.get("justification", ""),
                )
                return snapshot, 200

            return {"error": "not_found", "message": "未知路由"}, 404

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    server = make_server(ConsentBackend(), port=8080)
    print("未成年人隐私同意后端监听于 127.0.0.1:8080")
    server.serve_forever()


if __name__ == "__main__":
    main()
