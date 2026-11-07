"""抖音直播推送用到的 protobuf 解析。

抖音的推送帧是 protobuf 编码的，但插件只用到几个字段，手写一个最小的
wire-format 解析器就够了：不必引入 protobuf 运行时，也不用生成代码，
并且字段缺失时天然不会崩。

wire format 参考：https://protobuf.dev/programming-guides/encoding/
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# wire type 取值
_WT_VARINT = 0
_WT_FIXED64 = 1
_WT_BYTES = 2
_WT_FIXED32 = 5


def read_varint(data: bytes, pos: int) -> Tuple[int, int]:
    """从 pos 开始读一个 varint，返回（值, 新的位置）。"""

    result = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def iter_fields(data: bytes) -> List[Tuple[int, Any]]:
    """把一段 protobuf 消息拆成（字段号, 值）列表。

    值类型按 wire type 决定：varint 解成 int，定长字段保留原始字节，
    byte 字段解成 bytes（嵌套消息也走这里）。
    """

    fields: List[Tuple[int, Any]] = []
    pos = 0
    size = len(data)
    while pos < size:
        key, pos = read_varint(data, pos)
        field_number = key >> 3
        wire_type = key & 0x7
        if wire_type == _WT_VARINT:
            value, pos = read_varint(data, pos)
            fields.append((field_number, value))
        elif wire_type == _WT_FIXED64:
            fields.append((field_number, data[pos : pos + 8]))
            pos += 8
        elif wire_type == _WT_BYTES:
            length, pos = read_varint(data, pos)
            fields.append((field_number, data[pos : pos + length]))
            pos += length
        elif wire_type == _WT_FIXED32:
            fields.append((field_number, data[pos : pos + 4]))
            pos += 4
        else:
            raise ValueError(f"不支持的 protobuf wire type: {wire_type}")
    return fields


def get_value(data: bytes, field_number: int) -> Any:
    """返回第一个匹配字段的值；不存在时返回 None。"""

    for number, value in iter_fields(data):
        if number == field_number:
            return value
    return None


def get_all(data: bytes, field_number: int) -> List[Any]:
    """返回所有匹配字段的值（用于 repeated 字段）。"""

    return [value for number, value in iter_fields(data) if number == field_number]


def get_str(data: bytes, field_number: int) -> str:
    """读取一个字符串字段；缺失或类型不符时返回空字符串。"""

    value = get_value(data, field_number)
    return value.decode("utf-8", "ignore") if isinstance(value, bytes) else ""


def get_int(data: bytes, field_number: int) -> int:
    """读取一个整数字段；缺失时返回 0。"""

    value = get_value(data, field_number)
    return value if isinstance(value, int) else 0


def encode_varint(value: int) -> bytes:
    """把整数编码成 varint 字节。"""

    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def encode_bytes_field(field_number: int, payload: bytes) -> bytes:
    """编码一个 byte 类型字段（tag + 长度 + 载荷）。"""

    return bytes([(field_number << 3) | _WT_BYTES]) + encode_varint(len(payload)) + payload


def encode_int_field(field_number: int, value: int) -> bytes:
    """编码一个 varint 类型字段。"""

    return bytes([(field_number << 3) | _WT_VARINT]) + encode_varint(value)


def decode_user(data: bytes) -> Dict[str, Any]:
    """解出 User 结构的昵称、ID 与头像。

    抖音 User 的字段号：id=1、nickName=3、AvatarThumb=9。
    """

    return {
        "id": str(get_int(data, 1) or ""),
        "nickname": get_str(data, 3),
        "avatar": get_str(get_value(data, 9) or b"", 1) if get_value(data, 9) else "",
    }
