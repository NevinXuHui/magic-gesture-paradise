#!/usr/bin/env python3
"""向 SmartApp Runtime 发送 JSONL 命令或持续订阅事件。"""

import argparse
import json
import math
import socket
import sys
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Sequence, Tuple


class ClientError(Exception):
    pass


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result = {}  # type: Dict[str, Any]
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number: " + value)


def _parse_json_object(raw: str) -> Dict[str, Any]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError) as error:
        raise ClientError("JSON 输入无效: {0}".format(error)) from None
    if type(value) is not dict:
        raise ClientError("JSON 输入必须是对象")
    return value


def _parse_request(raw: str) -> Dict[str, Any]:
    value = _parse_json_object(raw)
    request_id = value.get("requestId")
    if (
        type(request_id) is not str
        or not 1 <= len(request_id) <= 128
        or any(not 0x20 <= ord(character) <= 0x7E for character in request_id)
    ):
        raise ClientError("requestId 必须是 1..128 位可打印 ASCII 字符串")
    return value


def _read_source(arguments: argparse.Namespace) -> str:
    if arguments.json_text is not None:
        return arguments.json_text
    if arguments.file is not None:
        try:
            return Path(arguments.file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ClientError("无法读取 JSON 文件: {0}".format(error)) from None
    return sys.stdin.read()


def _recv_line(stream: BinaryIO, maximum: int) -> bytes:
    line = stream.readline(maximum + 1)
    if not line:
        raise ClientError("在收到对应 command_result 前连接已关闭")
    if len(line) > maximum:
        raise ClientError("服务端 JSONL 帧超过字节限制")
    if not line.endswith(b"\n"):
        raise ClientError("服务端返回了不完整的 JSONL 帧")
    return line[:-1]


def _decode_response(payload: bytes) -> Dict[str, Any]:
    try:
        return _parse_json_object(payload.decode("utf-8"))
    except UnicodeDecodeError:
        raise ClientError("服务端响应不是 UTF-8") from None


def _matches_subscription(
    response: Dict[str, Any],
    event: Optional[str],
    data_type: Optional[str],
    app_id: Optional[str],
) -> bool:
    return (
        (event is None or response.get("event") == event)
        and (data_type is None or response.get("dataType") == data_type)
        and (app_id is None or response.get("appId") == app_id)
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="发送 SmartApp Runtime JSONL 命令或持续订阅事件"
    )
    parser.add_argument("--socket", required=True, help="Runtime Unix 套接字路径")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--json", dest="json_text", help="JSON 对象字符串")
    source.add_argument("--file", help="UTF-8 JSON 文件；未指定 --json/--file 时读 stdin")
    source.add_argument("--listen", action="store_true", help="保持连接并持续输出事件")
    parser.add_argument("--timeout", type=float, default=10.0, help="套接字超时秒数（默认 10）")
    parser.add_argument(
        "--max-line-bytes", type=int, default=1048576,
        help="单条输入/输出 JSONL 字节上限（默认 1048576）",
    )
    parser.add_argument("--event", help="监听模式下按 event 过滤")
    parser.add_argument("--data-type", help="监听模式下按 dataType 过滤")
    parser.add_argument("--app-id", help="监听模式下按 appId 过滤")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    if (
        not math.isfinite(arguments.timeout)
        or arguments.timeout <= 0
        or arguments.max_line_bytes <= 0
    ):
        print("agent_client: timeout 和 max-line-bytes 必须为正数", file=sys.stderr)
        return 2
    if not arguments.listen and any((arguments.event, arguments.data_type, arguments.app_id)):
        print("agent_client: --event/--data-type/--app-id 只能与 --listen 一起使用", file=sys.stderr)
        return 2
    try:
        if arguments.listen:
            request = None
            request_id = None
            encoded = None
        else:
            request = _parse_request(_read_source(arguments))
            request_id = request["requestId"]
            encoded = json.dumps(
                request, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8") + b"\n"
            if len(encoded) > arguments.max_line_bytes:
                raise ClientError("命令 JSONL 帧超过字节限制")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(arguments.timeout)
            connection.connect(arguments.socket)
            if encoded is not None:
                connection.sendall(encoded)
            else:
                connection.settimeout(None)
            with connection.makefile("rb") as stream:
                while True:
                    response = _decode_response(
                        _recv_line(stream, arguments.max_line_bytes)
                    )
                    if arguments.listen:
                        if (
                            response.get("event") == "command_result"
                            and response.get("requestId") == "connection"
                            and response.get("ok") is False
                        ):
                            message = response.get("error", {}).get("message", "连接被拒绝")
                            raise ClientError(str(message))
                        if not _matches_subscription(
                            response, arguments.event, arguments.data_type, arguments.app_id
                        ):
                            continue
                    print(
                        json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                        flush=True,
                    )
                    if (
                        not arguments.listen
                        and response.get("event") == "command_result"
                        and response.get("requestId") == request_id
                    ):
                        return 0 if response.get("ok") is True else 1
    except KeyboardInterrupt:
        return 0
    except (ClientError, OSError, socket.timeout, ValueError) as error:
        print("agent_client: {0}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
