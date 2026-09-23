"""
Hermes Bridge 工具函数

从 Hermes XiaoliChannel 适配器提取的核心工具函数。
"""
import re
import textwrap
from typing import List


# ══════════════════════════════════════════════════════════════════════════════
# Markdown 格式化工具
# ══════════════════════════════════════════════════════════════════════════════

_FENCE_RE = re.compile(r"^```([^\n`]*)\s*$")
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_TABLE_RULE_RE = re.compile(
    r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$"
)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MENTION_STRIP_RE = re.compile(r"^@\S+\s*")
_XIAOLI_COPY_LINE_WIDTH = 120


def normalize_markdown_blocks(content: str) -> str:
    """
    折叠多余的空行，去除行尾空白，保护代码块。

    Args:
        content: 原始 Markdown 文本

    Returns:
        标准化后的文本
    """
    if not content:
        return content

    lines = content.splitlines()
    out = []
    in_fence = False
    fence_marker = ""
    blank_count = 0

    for line in lines:
        # 检测代码块边界
        if not in_fence:
            fence_match = _FENCE_RE.match(line)
            if fence_match:
                in_fence = True
                fence_marker = fence_match.group(1) or ""
                out.append(line)
                blank_count = 0
                continue
        else:
            # 检查代码块结束
            if line.strip() == "```":
                in_fence = False
                out.append(line)
                blank_count = 0
                continue

        # 在代码块内：保留所有内容
        if in_fence:
            out.append(line)
            continue

        # 在代码块外：折叠多余空行
        stripped = line.rstrip()
        if not stripped:
            blank_count += 1
            # 最多保留一个空行
            if blank_count <= 1:
                out.append("")
        else:
            blank_count = 0
            out.append(stripped)

    # 去除开头和结尾的空行
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()

    return "\n".join(out)


def wrap_copy_friendly_lines(content: str, max_width: int = _XIAOLI_COPY_LINE_WIDTH) -> str:
    """
    换行过长的文本行，保护代码块、表格、链接。

    Args:
        content: Markdown 文本
        max_width: 最大行宽（默认 120）

    Returns:
        换行后的文本
    """
    if not content:
        return content

    lines = content.splitlines()
    out = []
    in_fence = False
    in_table = False

    for line in lines:
        # 检测代码块
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue

        # 代码块内不换行
        if in_fence:
            out.append(line)
            continue

        # 检测表格
        if "|" in line or _TABLE_RULE_RE.match(line):
            in_table = True
            out.append(line)
            continue
        elif in_table and not line.strip():
            in_table = False
            out.append(line)
            continue
        elif in_table:
            out.append(line)
            continue

        # 检测 Markdown 标题
        if _HEADER_RE.match(line):
            out.append(line)
            continue

        # 检测 Markdown 链接（不换行）
        if _MARKDOWN_LINK_RE.search(line):
            out.append(line)
            continue

        # 普通文本：如果太长则换行
        if len(line) <= max_width:
            out.append(line)
        else:
            # 使用 textwrap 智能换行
            wrapped = textwrap.fill(
                line,
                width=max_width,
                break_long_words=False,
                break_on_hyphens=False
            )
            out.append(wrapped)

    return "\n".join(out)


def format_message_for_xiaolichannel(content: str) -> str:
    """
    格式化消息以适配 XiaoliChannel。

    综合应用：
    1. 折叠多余空行
    2. 去除行尾空白
    3. 换行过长文本
    4. 保护代码块和表格

    Args:
        content: 原始消息内容

    Returns:
        格式化后的消息
    """
    if not content:
        return content

    normalized = normalize_markdown_blocks(content)
    wrapped = wrap_copy_friendly_lines(normalized)
    return wrapped


# ══════════════════════════════════════════════════════════════════════════════
# 文本处理工具
# ══════════════════════════════════════════════════════════════════════════════

def strip_mention(text: str) -> str:
    """
    移除消息开头的 @提及。

    仅移除行首的 @mention，避免误伤中间的 @（如邮箱、GitHub URL）。

    Args:
        text: 原始文本

    Returns:
        移除提及后的文本
    """
    if not text:
        return text

    stripped = _MENTION_STRIP_RE.sub("", text, count=1).strip()
    # 如果移除后为空，返回原文本
    return stripped or text


def safe_id(text: str, max_len: int = 20) -> str:
    """
    生成安全的 ID 字符串（用于日志）。

    Args:
        text: 原始文本
        max_len: 最大长度

    Returns:
        安全的 ID 字符串
    """
    if not text:
        return "(empty)"

    # 去除不可打印字符
    safe = "".join(c if c.isprintable() else "_" for c in text)

    # 截断过长的文本
    if len(safe) > max_len:
        return safe[:max_len] + "..."

    return safe


def split_text(text: str, max_length: int = 4096) -> List[str]:
    """
    将长文本分割为多个片段（每个不超过 max_length）。

    优先在段落边界分割，避免截断代码块。

    Args:
        text: 原始文本
        max_length: 每个片段的最大长度

    Returns:
        文本片段列表
    """
    if not text or len(text) <= max_length:
        return [text] if text else []

    chunks = []
    current_chunk = ""

    # 按段落分割（保留双换行）
    paragraphs = text.split("\n\n")

    for para in paragraphs:
        # 如果加上当前段落会超长
        if len(current_chunk) + len(para) + 2 > max_length:
            # 保存当前块
            if current_chunk:
                chunks.append(current_chunk.strip())

            # 如果单个段落太长，按行分割
            if len(para) > max_length:
                lines = para.splitlines()
                temp_chunk = ""
                for line in lines:
                    if len(temp_chunk) + len(line) + 1 > max_length:
                        if temp_chunk:
                            chunks.append(temp_chunk.strip())
                        temp_chunk = line
                    else:
                        temp_chunk += ("\n" if temp_chunk else "") + line
                current_chunk = temp_chunk
            else:
                current_chunk = para
        else:
            current_chunk += ("\n\n" if current_chunk else "") + para

    # 保存最后一块
    if current_chunk:
        chunks.append(current_chunk.strip())

    return chunks


# ══════════════════════════════════════════════════════════════════════════════
# 访问控制工具
# ══════════════════════════════════════════════════════════════════════════════

def is_stale_session_error(ret: int, errcode: int, errmsg: str) -> bool:
    """
    检测是否是过期会话错误（ret=-2, errmsg='unknown error'）。

    这不是真正的速率限制，而是会话过期的信号。

    Args:
        ret: API 返回的 ret 字段
        errcode: API 返回的 errcode 字段
        errmsg: API 返回的 errmsg 字段

    Returns:
        True 如果是过期会话错误
    """
    RATE_LIMIT_ERRCODE = -2
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False

    return (errmsg or "").lower() == "unknown error"


def compute_content_fingerprint(user_id: str, text: str) -> str:
    """
    计算消息内容指纹（用于去重）。

    Args:
        user_id: 用户 ID
        text: 消息文本

    Returns:
        内容指纹（SHA256 哈希）
    """
    import hashlib
    content = f"{user_id}:{text}"
    return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]
