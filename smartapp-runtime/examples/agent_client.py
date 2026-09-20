#!/usr/bin/env python3
"""向 SmartApp Runtime Unix 套接字发送一条 JSONL 命令。"""

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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="发送一条 SmartApp Runtime JSONL 命令并等待对应结果"
    )
    parser.add_argument("--socket", required=True, help="Runtime Unix 套接字路径")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--json", dest="json_text", help="JSON 对象字符串")
    source.add_argument("--file", help="UTF-8 JSON 文件；未指定 --json/--file 时读 stdin")
    parser.add_argument("--timeout", type=float, default=10.0, help="套接字超时秒数（默认 10）")
    parser.add_argument(
        "--max-line-bytes", type=int, default=1048576,
        help="单条输入/输出 JSONL 字节上限（默认 1048576）",
    )
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
    try:
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
            connection.sendall(encoded)
            with connection.makefile("rb") as stream:
                while True:
                    response = _decode_response(
                        _recv_line(stream, arguments.max_line_bytes)
                    )
                    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
                    if (
                        response.get("event") == "command_result"
                        and response.get("requestId") == request_id
                    ):
                        return 0 if response.get("ok") is True else 1
    except (ClientError, OSError, socket.timeout, ValueError) as error:
        print("agent_client: {0}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
