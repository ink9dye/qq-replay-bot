from __future__ import annotations
import ctypes, lzma, sqlite3, struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


REPLAY_YRP1 = 0x31707279

REPLAY_YRP2 = 0x32707279

REPLAY_COMPRESSED = 0x1

REPLAY_TAG = 0x2

REPLAY_SINGLE = 0x8

REPLAY_UNIFORM = 0x10

MSG_RETRY = 1

MSG_HINT = 2

MSG_WIN = 5

MSG_UPDATE_DATA = 6

MSG_UPDATE_CARD = 7

MSG_SELECT_BATTLECMD = 10

MSG_SELECT_IDLECMD = 11

MSG_SELECT_EFFECTYN = 12

MSG_SELECT_YESNO = 13

MSG_SELECT_OPTION = 14

MSG_SELECT_CARD = 15

MSG_SELECT_CHAIN = 16

MSG_SELECT_PLACE = 18

MSG_SELECT_POSITION = 19

MSG_SELECT_TRIBUTE = 20

MSG_SELECT_COUNTER = 22

MSG_SELECT_SUM = 23

MSG_SELECT_DISFIELD = 24

MSG_SORT_CARD = 25

MSG_SELECT_UNSELECT_CARD = 26

MSG_CONFIRM_DECKTOP = 30

MSG_CONFIRM_CARDS = 31

MSG_SHUFFLE_DECK = 32

MSG_SHUFFLE_HAND = 33

MSG_REFRESH_DECK = 34

MSG_SWAP_GRAVE_DECK = 35

MSG_SHUFFLE_SET_CARD = 36

MSG_REVERSE_DECK = 37

MSG_DECK_TOP = 38

MSG_SHUFFLE_EXTRA = 39

MSG_NEW_TURN = 40

MSG_NEW_PHASE = 41

MSG_CONFIRM_EXTRATOP = 42

MSG_MOVE = 50

MSG_POS_CHANGE = 53

MSG_SET = 54

MSG_SWAP = 55

MSG_FIELD_DISABLED = 56

MSG_SUMMONING = 60

MSG_SUMMONED = 61

MSG_SPSUMMONING = 62

MSG_SPSUMMONED = 63

MSG_FLIPSUMMONING = 64

MSG_FLIPSUMMONED = 65

MSG_CHAINING = 70

MSG_CHAINED = 71

MSG_CHAIN_SOLVING = 72

MSG_CHAIN_SOLVED = 73

MSG_CHAIN_END = 74

MSG_CHAIN_NEGATED = 75

MSG_CHAIN_DISABLED = 76

MSG_CARD_SELECTED = 80

MSG_RANDOM_SELECTED = 81

MSG_BECOME_TARGET = 83

MSG_DRAW = 90

MSG_DAMAGE = 91

MSG_RECOVER = 92

MSG_EQUIP = 93

MSG_LPUPDATE = 94

MSG_UNEQUIP = 95

MSG_CARD_TARGET = 96

MSG_CANCEL_TARGET = 97

MSG_PAY_LPCOST = 100

MSG_ADD_COUNTER = 101

MSG_REMOVE_COUNTER = 102

MSG_ATTACK = 110

MSG_BATTLE = 111

MSG_ATTACK_DISABLED = 112

MSG_DAMAGE_STEP_START = 113

MSG_DAMAGE_STEP_END = 114

MSG_MISSED_EFFECT = 120

MSG_TOSS_COIN = 130

MSG_TOSS_DICE = 131

MSG_ROCK_PAPER_SCISSORS = 132

MSG_HAND_RES = 133

MSG_ANNOUNCE_RACE = 140

MSG_ANNOUNCE_ATTRIB = 141

MSG_ANNOUNCE_CARD = 142

MSG_ANNOUNCE_NUMBER = 143

MSG_CARD_HINT = 160

MSG_PLAYER_HINT = 165

MSG_MATCH_KILL = 170

RESPONSE_MSGS = {
    MSG_RETRY,
    MSG_SELECT_BATTLECMD,
    MSG_SELECT_IDLECMD,
    MSG_SELECT_EFFECTYN,
    MSG_SELECT_YESNO,
    MSG_SELECT_OPTION,
    MSG_SELECT_CARD,
    MSG_SELECT_CHAIN,
    MSG_SELECT_PLACE,
    MSG_SELECT_POSITION,
    MSG_SELECT_TRIBUTE,
    MSG_SELECT_COUNTER,
    MSG_SELECT_SUM,
    MSG_SELECT_DISFIELD,
    MSG_SORT_CARD,
    MSG_SELECT_UNSELECT_CARD,
    MSG_ROCK_PAPER_SCISSORS,
    MSG_ANNOUNCE_RACE,
    MSG_ANNOUNCE_ATTRIB,
    MSG_ANNOUNCE_CARD,
    MSG_ANNOUNCE_NUMBER,
}

LOCATION_DECK = 0x01

LOCATION_HAND = 0x02

LOCATION_MZONE = 0x04

LOCATION_SZONE = 0x08

LOCATION_EXTRA = 0x40

POS_FACEUP_ATTACK = 0x1

POS_FACEDOWN_DEFENSE = 0xA

POS_FACEUP = 0x5

TYPE_MONSTER = 0x1

TYPE_LINK = 0x4000000

TYPES_EXTRA = 0x40 | 0x2000 | 0x800000 | TYPE_LINK

QUERY_CODE = 0x1

QUERY_POSITION = 0x2

QUERY_ATTACK = 0x100

QUERY_DEFENSE = 0x200

QUERY_TYPE = 0x8

QUERY_LINK = 0x800000

PROCESSOR_BUFFER_LEN = 0x0FFFFFFF

PROCESSOR_WAITING = 0x10000000

PROCESSOR_END = 0x20000000

MZONE_SIZE = 7

SZONE_SIZE = 8

class ReplayError(Exception):
    pass

class Cursor:
    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def u8(self) -> int:
        if self.pos >= len(self.data):
            raise ReplayError(f"录像在偏移 {self.pos} 处提前结束")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def skip(self, size: int) -> None:
        if size < 0 or self.pos + size > len(self.data):
            raise ReplayError(f"录像在偏移 {self.pos} 处还需要 {size} 字节")
        self.pos += size

    def take(self, size: int) -> bytes:
        start = self.pos
        self.skip(size)
        return self.data[start:self.pos]

@dataclass
class Message:
    type: int
    body: bytes
    response: Optional[bytes] = None

    @property
    def player(self) -> int:
        if self.type == MSG_SELECT_SUM and self.body:
            return self.body[1]
        if self.body:
            return self.body[0]
        return -1

@dataclass
class Replay:
    path: Path
    flag: int
    seed: int
    seeds: list[int]
    names: list[str]
    start_lp: int
    start_hand: int
    draw_count: int
    duel_flag: int
    main: list[list[int]]
    extra: list[list[int]]
    responses: list[bytes]

@dataclass
class CardRow:
    code: int
    alias: int
    setcode: int
    type: int
    attack: int
    defense: int
    level: int
    race: int
    attribute: int

class CardDB:
    def __init__(self, root: Path):
        self.rows: dict[int, CardRow] = {}
        self.names: dict[int, str] = {}
        files = [root / "cards.cdb"]
        expansions = root / "expansions"
        if expansions.is_dir():
            files.extend(sorted(expansions.glob("*.cdb")))
        for path in files:
            if path.is_file():
                self._load(path)

    def _load(self, path: Path) -> None:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            for row in con.execute(
                "select id, alias, setcode, type, atk, def, level, race, attribute from datas"
            ):
                self.rows[row[0]] = CardRow(*row)
            for code, name in con.execute("select id, name from texts"):
                if name:
                    self.names[code] = name
        finally:
            con.close()

    def name(self, code: int) -> str:
        return self.names.get(code) or str(code)

    def resolve(self, text: str) -> int:
        text = text.strip()
        if text.isdigit():
            code = int(text)
            if code not in self.rows:
                raise ReplayError(f"卡密 {code} 不在卡片数据库里")
            return code
        exact = [code for code, name in self.names.items() if name == text]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ReplayError(f"卡名 {text} 对应多张卡：{exact[:6]}")
        fuzzy = [code for code, name in self.names.items() if text in name]
        if len(fuzzy) == 1:
            return fuzzy[0]
        if not fuzzy:
            raise ReplayError(f"找不到卡：{text}")
        shown = "、".join(self.names[code] for code in fuzzy[:8])
        raise ReplayError(f"卡名 {text} 不唯一，请写完整名字或卡密：{shown}")

def decompress_replay(blob: bytes) -> bytes:
    if len(blob) < 32:
        raise ReplayError("录像太短")
    magic, _version, flag, _seed, datasize = struct.unpack_from("<IIIII", blob, 0)
    if magic not in (REPLAY_YRP1, REPLAY_YRP2):
        raise ReplayError("不是 YGOPro 录像")
    if not 0 < datasize <= 0x80000:
        raise ReplayError("录像解压大小超出范围")
    header_size = 80 if magic == REPLAY_YRP2 else 32
    if len(blob) < header_size:
        raise ReplayError("录像头不完整")
    if not (flag & REPLAY_COMPRESSED):
        data = blob[header_size:header_size + datasize]
        if len(data) != datasize:
            raise ReplayError("录像正文不完整")
        return data
    props = blob[24:29]
    if len(props) < 5 or props[0] == 0:
        raise ReplayError("压缩录像头损坏")
    dictionary = struct.unpack_from("<I", props, 1)[0]
    if not 4096 <= dictionary <= 64 * 1024 * 1024:
        raise ReplayError("录像压缩字典超出范围")
    encoded = props[0]
    pb = encoded // 45
    rest = encoded % 45
    lp = rest // 9
    lc = rest % 9
    decompressor = lzma.LZMADecompressor(
        format=lzma.FORMAT_RAW,
        filters=[{"id": lzma.FILTER_LZMA1, "dict_size": dictionary, "lc": lc, "lp": lp, "pb": pb}],
    )
    data = decompressor.decompress(blob[header_size:], max_length=datasize + 1)
    if len(data) != datasize:
        raise ReplayError("录像解压后的长度不对")
    return data

def _read_deck(cur: Cursor) -> list[int]:
    count = struct.unpack_from("<I", cur.take(4))[0]
    if count > 80:
        raise ReplayError(f"卡组张数异常：{count}")
    if count == 0:
        return []
    return list(struct.unpack_from(f"<{count}I", cur.take(count * 4)))

def parse_replay(path: Path) -> Replay:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ReplayError("录像文件过大")
    blob = path.read_bytes()
    if len(blob) < 32:
        raise ReplayError("录像头不完整")
    magic, _version, flag, seed, _datasize, _start = struct.unpack_from("<IIIIII", blob, 0)
    if flag & REPLAY_SINGLE:
        raise ReplayError(f"{path.name} 是解谜录像，需要双人对战录像")
    if flag & REPLAY_TAG:
        raise ReplayError(f"{path.name} 是双打录像，目前只推演单打")
    seeds = list(struct.unpack_from("<8I", blob, 32)) if magic == REPLAY_YRP2 and len(blob) >= 64 else []
    data = decompress_replay(blob)
    player_count = 2
    cur = Cursor(data)
    names = []
    for _ in range(player_count):
        raw = cur.take(40)
        names.append(raw.decode("utf-16le", "replace").split("\x00", 1)[0])
    start_lp, start_hand, draw_count, duel_flag = struct.unpack("<iiii", cur.take(16))
    if not 1 <= start_lp <= 1000000 or not 0 <= start_hand <= 80 or not 0 <= draw_count <= 80:
        raise ReplayError("录像初始参数异常")
    main = []
    extra = []
    for _ in range(player_count):
        main.append(_read_deck(cur))
        extra.append(_read_deck(cur))
    responses = parse_responses(data[cur.pos:])
    return Replay(path, flag, seed, seeds, names, start_lp, start_hand, draw_count, duel_flag, main, extra, responses)

def parse_responses(stream: bytes) -> list[bytes]:
    """录像在卡组之后只记录每次回答，不记录场面消息。"""
    cur = Cursor(stream)
    found: list[bytes] = []
    while cur.pos < len(stream):
        length = cur.u8()
        if not 1 <= length <= 64:
            raise ReplayError("录像响应长度异常")
        found.append(cur.take(length))
    return found

def parse_message(cur: Cursor) -> Message:
    start = cur.pos
    msg = cur.u8()
    if msg == MSG_HINT:
        cur.skip(6)
    elif msg == MSG_WIN:
        cur.skip(2)
    elif msg == MSG_SELECT_BATTLECMD:
        cur.u8()
        count = cur.u8()
        cur.skip(count * 11)
        count = cur.u8()
        cur.skip(count * 8 + 2)
    elif msg == MSG_SELECT_IDLECMD:
        cur.u8()
        for _ in range(5):
            count = cur.u8()
            cur.skip(count * 7)
        count = cur.u8()
        cur.skip(count * 11 + 3)
    elif msg == MSG_SELECT_EFFECTYN:
        cur.skip(13)
    elif msg == MSG_SELECT_YESNO:
        cur.skip(5)
    elif msg == MSG_SELECT_OPTION:
        cur.u8()
        count = cur.u8()
        cur.skip(count * 4)
    elif msg in (MSG_SELECT_CARD, MSG_SELECT_TRIBUTE):
        cur.skip(4)
        count = cur.u8()
        cur.skip(count * 8)
    elif msg == MSG_SELECT_CHAIN:
        cur.u8()
        count = cur.u8()
        cur.skip(9 + count * 14)
    elif msg in (MSG_SELECT_PLACE, MSG_SELECT_DISFIELD, MSG_SELECT_POSITION):
        cur.skip(6)
    elif msg == MSG_SELECT_COUNTER:
        cur.skip(5)
        count = cur.u8()
        cur.skip(count * 9)
    elif msg == MSG_SELECT_SUM:
        cur.skip(8)
        count = cur.u8()
        cur.skip(count * 11)
        count = cur.u8()
        cur.skip(count * 11)
    elif msg == MSG_SELECT_UNSELECT_CARD:
        cur.skip(5)
        count = cur.u8()
        cur.skip(count * 8)
        count = cur.u8()
        cur.skip(count * 8)
    elif msg == MSG_SORT_CARD:
        cur.u8()
        count = cur.u8()
        cur.skip(count * 7)
    elif msg in (MSG_CONFIRM_DECKTOP, MSG_CONFIRM_EXTRATOP):
        cur.u8()
        count = cur.u8()
        cur.skip(count * 7)
    elif msg == MSG_CONFIRM_CARDS:
        cur.skip(2)
        count = cur.u8()
        cur.skip(count * 7)
    elif msg == MSG_SHUFFLE_DECK:
        cur.skip(1)
    elif msg in (MSG_SHUFFLE_HAND, MSG_SHUFFLE_EXTRA):
        cur.u8()
        count = cur.u8()
        cur.skip(count * 4)
    elif msg in (MSG_REFRESH_DECK, MSG_SWAP_GRAVE_DECK, MSG_NEW_TURN):
        cur.skip(1)
    elif msg == MSG_SHUFFLE_SET_CARD:
        cur.u8()
        count = cur.u8()
        cur.skip(count * 8)
    elif msg == MSG_DECK_TOP:
        cur.skip(6)
    elif msg == MSG_NEW_PHASE:
        cur.skip(2)
    elif msg in (MSG_MOVE, MSG_SWAP, MSG_CHAINING):
        cur.skip(16)
    elif msg == MSG_POS_CHANGE:
        cur.skip(9)
    elif msg == MSG_SET:
        cur.skip(8)
    elif msg in (MSG_FIELD_DISABLED, MSG_MATCH_KILL):
        cur.skip(4)
    elif msg in (MSG_SUMMONING, MSG_SPSUMMONING, MSG_FLIPSUMMONING, MSG_MISSED_EFFECT, MSG_EQUIP, MSG_ATTACK, MSG_CARD_TARGET, MSG_CANCEL_TARGET):
        cur.skip(8)
    elif msg in (MSG_SUMMONED, MSG_SPSUMMONED, MSG_FLIPSUMMONED, MSG_REVERSE_DECK, MSG_CHAIN_END, MSG_ATTACK_DISABLED, MSG_DAMAGE_STEP_START, MSG_DAMAGE_STEP_END, MSG_RETRY):
        pass
    elif msg in (MSG_CHAINED, MSG_CHAIN_SOLVING, MSG_CHAIN_SOLVED, MSG_CHAIN_NEGATED, MSG_CHAIN_DISABLED, MSG_HAND_RES):
        cur.skip(1)
    elif msg in (MSG_CARD_SELECTED, MSG_RANDOM_SELECTED):
        cur.u8()
        count = cur.u8()
        cur.skip(count * 4)
    elif msg == MSG_BECOME_TARGET:
        count = cur.u8()
        cur.skip(count * 4)
    elif msg == MSG_DRAW:
        cur.u8()
        count = cur.u8()
        cur.skip(count * 4)
    elif msg in (MSG_DAMAGE, MSG_RECOVER, MSG_LPUPDATE, MSG_PAY_LPCOST):
        cur.skip(5)
    elif msg == MSG_UNEQUIP:
        cur.skip(4)
    elif msg in (MSG_ADD_COUNTER, MSG_REMOVE_COUNTER):
        cur.skip(7)
    elif msg == MSG_BATTLE:
        cur.skip(26)
    elif msg in (MSG_TOSS_COIN, MSG_TOSS_DICE):
        cur.u8()
        count = cur.u8()
        cur.skip(count)
    elif msg == MSG_ROCK_PAPER_SCISSORS:
        cur.skip(1)
    elif msg in (MSG_ANNOUNCE_RACE, MSG_ANNOUNCE_ATTRIB):
        cur.skip(6)
    elif msg in (MSG_ANNOUNCE_CARD, MSG_ANNOUNCE_NUMBER):
        cur.u8()
        count = cur.u8()
        cur.skip(count * 4)
    elif msg == MSG_CARD_HINT:
        cur.skip(9)
    elif msg == MSG_PLAYER_HINT:
        cur.skip(6)
    elif msg == MSG_UPDATE_CARD:
        cur.skip(3)
        length = struct.unpack_from("<I", cur.take(4))[0]
        if length > 4:
            cur.skip(length - 4)
    elif msg == MSG_UPDATE_DATA:
        player_byte = cur.u8()
        location = cur.u8()
        slots = {LOCATION_MZONE: MZONE_SIZE, LOCATION_SZONE: SZONE_SIZE}.get(location)
        if slots is None:
            raise ReplayError(f"无法识别的刷新区域 {location}（玩家标记 {player_byte}）")
        for _ in range(slots):
            length = struct.unpack_from("<I", cur.take(4))[0]
            if length > 4:
                cur.skip(length - 4)
    else:
        raise ReplayError(f"录像里有未识别的消息 {msg}，偏移 {start}")
    return Message(msg, cur.data[start + 1:cur.pos])

class CardData(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_uint32),
        ("alias", ctypes.c_uint32),
        ("setcode", ctypes.c_uint16 * 16),
        ("type", ctypes.c_uint32),
        ("level", ctypes.c_uint32),
        ("attribute", ctypes.c_uint32),
        ("race", ctypes.c_uint32),
        ("attack", ctypes.c_int32),
        ("defense", ctypes.c_int32),
        ("lscale", ctypes.c_uint32),
        ("rscale", ctypes.c_uint32),
        ("link_marker", ctypes.c_uint32),
        ("rule_code", ctypes.c_uint32),
    ]

class Core:
    def __init__(self, dll: Path, cards: CardDB, root: Path):
        if ctypes.sizeof(CardData) != 80:
            raise ReplayError("卡片数据结构大小不对，和当前 ocgcore 不匹配")
        self.cards = cards
        self.scripts: dict[str, bytes] = {}
        self.script_dirs = [root / "expansions" / "script", root / "script"]
        self.errors: list[str] = []
        self.script_hashes = {}
        self.missing_scripts = set()
        self.lib = (getattr(ctypes, 'WinDLL', None) or ctypes.CDLL)(str(dll))
        self._bind()
        self.script_buf = ctypes.create_string_buffer(0x100000)
        self._script_cb = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int))(self._read_script)
        self._card_cb = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(CardData))(self._read_card)
        self._msg_cb = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32)(self._handle_message)
        self.lib.set_script_reader(self._script_cb)
        self.lib.set_card_reader(self._card_cb)
        self.lib.set_message_handler(self._msg_cb)

    def _bind(self) -> None:
        lib = self.lib
        void_p = ctypes.c_void_p
        lib.create_duel.restype = void_p
        lib.create_duel.argtypes = [ctypes.c_uint32]
        lib.create_duel_v2.restype = void_p
        lib.create_duel_v2.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
        lib.start_duel.argtypes = [void_p, ctypes.c_uint32]
        lib.end_duel.argtypes = [void_p]
        lib.set_player_info.argtypes = [void_p, ctypes.c_int32, ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
        lib.process.restype = ctypes.c_uint32
        lib.process.argtypes = [void_p]
        lib.get_message.restype = ctypes.c_int32
        lib.get_message.argtypes = [void_p, ctypes.c_void_p]
        lib.new_card.argtypes = [void_p, ctypes.c_uint32, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8]
        lib.set_responseb.argtypes = [void_p, ctypes.c_void_p]
        lib.query_card.restype = ctypes.c_int32
        lib.query_card.argtypes = [void_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int32]
        lib.query_field_count.restype = ctypes.c_int32
        lib.query_field_count.argtypes = [void_p, ctypes.c_uint8, ctypes.c_uint8]
        lib.get_log_message.argtypes = [void_p, ctypes.c_char_p]
        lib.preload_script.restype = ctypes.c_int32
        lib.preload_script.argtypes = [void_p, ctypes.c_char_p]

    def _script_bytes(self, name: str) -> Optional[bytes]:
        base = Path(name.replace("\\", "/")).name
        if base in self.scripts:
            return self.scripts[base]
        for folder in self.script_dirs:
            path = folder / base
            if path.is_file():
                data = path.read_bytes()
                self.scripts[base] = data
                return data
        self.scripts[base] = b""
        return None

    def _read_script(self, name, length):
        text = name.decode("utf-8", "replace") if name else ""
        data = self._script_bytes(text)
        if not data:
            length[0] = 0
            self.missing_scripts.add(text)
            return None
        if len(data) > len(self.script_buf):
            self.errors.append("卡片脚本过大")
            length[0] = 0
            return None
        import hashlib
        self.script_hashes[text] = hashlib.sha256(data).hexdigest()
        size = len(data)
        ctypes.memmove(self.script_buf, data, size)
        length[0] = size
        return ctypes.addressof(self.script_buf)

    def _read_card(self, code, data):
        ctypes.memset(data, 0, ctypes.sizeof(CardData))
        row = self.cards.rows.get(code)
        if row is None:
            self.errors.append(f'本机卡库缺少卡片：{code}')
            return 0
        card = data.contents
        card.code = code
        card.alias = row.alias or 0
        value = (row.setcode or 0) & 0xffffffffffffffff
        index = 0
        while value and index < 16:
            part = value & 0xFFFF
            if part:
                card.setcode[index] = part
                index += 1
            value >>= 16
        card.type = row.type or 0
        level = row.level or 0
        card.level = level & 0xFF
        card.lscale = (level >> 24) & 0xFF
        card.rscale = (level >> 16) & 0xFF
        card.attribute = row.attribute or 0
        card.race = row.race or 0
        card.attack = row.attack or 0
        if card.type & TYPE_LINK:
            card.link_marker = row.defense or 0
            card.defense = 0
        else:
            card.defense = row.defense or 0
        return 1

    def _handle_message(self, duel, _message_type):
        buf = ctypes.create_string_buffer(1024)
        self.lib.get_log_message(duel, buf)
        text = buf.value.decode("utf-8", "replace").strip()
        if text and text not in self.errors:
            self.errors.append(text)
        return 0

    def respond(self, duel, payload: bytes) -> None:
        buf = ctypes.create_string_buffer(256)
        size = min(len(payload), 256)
        if size:
            ctypes.memmove(buf, payload, size)
        self.lib.set_responseb(duel, buf)

    def messages(self, duel) -> tuple[list[Message], int]:
        result = self.lib.process(duel)
        length = result & PROCESSOR_BUFFER_LEN
        flag = result & 0xF0000000
        if not length:
            return [], flag
        if length > 2 * 1024 * 1024:
            raise ReplayError('内核消息超过支持范围')
        buf = ctypes.create_string_buffer(length)
        self.lib.get_message(duel, buf)
        cur = Cursor(bytes(buf.raw[:length]))
        found = []
        while cur.pos < length:
            found.append(parse_message(cur))
        return found, flag
