"""
T98 / H20P dashcam server  -  JT/T 808-2013 signalling + JT/T 1078-2016 live video.

Flow:
  device --TCP--> SIGNAL_PORT : register(0x0100) -> auth(0x0102) -> heartbeat / location
  server sends 0x9101 (live video request) for each channel
  device --TCP--> MEDIA_PORT  : 1078 RTP packets -> reassembled H.264 -> ffmpeg -> MediaMTX RTSP

Output stream: rtsp://<mediamtx>:8554/<device_id>_ch<N>   (read it with cv2 / GStreamer / your pipeline)

Run:
  python jt_server.py --public-ip <IP the dashcam can reach> --channels 1 2
Needs: Python 3.9+, ffmpeg in PATH, MediaMTX running (default rtsp://127.0.0.1:8554)
"""
import argparse
import asyncio
import logging
import struct
from datetime import datetime

log = logging.getLogger("jt")

# ------------------------------------------------------------------ JT808 framing
def unescape(data: bytes) -> bytes:
    return data.replace(b"\x7d\x02", b"\x7e").replace(b"\x7d\x01", b"\x7d")


def escape(data: bytes) -> bytes:
    return data.replace(b"\x7d", b"\x7d\x01").replace(b"\x7e", b"\x7d\x02")


def xor(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
    return c


def bcd_to_str(b: bytes) -> str:
    return b.hex()


def str_to_bcd(s: str, n: int) -> bytes:
    return bytes.fromhex(s.rjust(n * 2, "0"))


def bcd_time(b: bytes) -> str:
    s = b.hex()  # YYMMDDhhmmss, GMT+8
    return f"20{s[0:2]}-{s[2:4]}-{s[4:6]} {s[6:8]}:{s[8:10]}:{s[10:12]}"


def parse_808(frame: bytes):
    """frame = raw bytes between two 0x7E. Returns dict or None."""
    data = unescape(frame)
    if len(data) < 13 or xor(data[:-1]) != data[-1]:
        log.warning("bad checksum / short frame: %s", data.hex())
        return None
    msg_id, props = struct.unpack(">HH", data[0:4])
    phone = bcd_to_str(data[4:10])
    seq = struct.unpack(">H", data[10:12])[0]
    off = 12
    sub = bool(props & 0x2000)
    pkg = None
    if sub:
        pkg = struct.unpack(">HH", data[12:16])  # (total, index)
        off = 16
    body_len = props & 0x03FF
    body = data[off:off + body_len]
    return {"id": msg_id, "phone": phone, "seq": seq, "body": body, "sub": pkg}


def build_808(msg_id: int, phone: str, seq: int, body: bytes) -> bytes:
    head = struct.pack(">HH", msg_id, len(body) & 0x03FF) + str_to_bcd(phone, 6) + struct.pack(">H", seq)
    raw = head + body
    return b"\x7e" + escape(raw + bytes([xor(raw)])) + b"\x7e"


# ------------------------------------------------------------------ alarm names (alarm process docx)
ADAS = {1: "forward_collision", 2: "lane_departure", 3: "distance_too_close", 4: "pedestrian_collision",
        5: "frequent_lane_change", 6: "road_sign_overrun", 7: "obstacle", 0x10: "road_sign_event",
        0x11: "active_capture"}
DMS = {1: "fatigue", 2: "phone_call", 3: "smoking", 4: "distracted", 5: "driver_abnormal",
       0x10: "auto_capture", 0x11: "driver_change"}


def parse_location(body: bytes) -> dict:
    alarm, status, lat, lon, alt, spd, head = struct.unpack(">IIIIHHH", body[0:22])
    loc = {
        "alarm_flags": alarm,
        "acc_on": bool(status & 1),
        "fixed": bool(status & 2),
        "lat": lat / 1e6 * (-1 if status & 4 else 1),
        "lon": lon / 1e6 * (-1 if status & 8 else 1),
        "alt_m": alt,
        "speed_kmh": spd / 10,
        "heading": head,
        "time_gmt8": bcd_time(body[22:28]),
        "events": [],
    }
    i = 28
    while i + 2 <= len(body):
        eid, elen = body[i], body[i + 1]
        val = body[i + 2:i + 2 + elen]
        i += 2 + elen
        if eid in (0x64, 0x65) and len(val) >= 7:
            alarm_id, flag, etype, level = struct.unpack(">IBBB", val[0:7])
            names = ADAS if eid == 0x64 else DMS
            loc["events"].append({
                "src": "ADAS" if eid == 0x64 else "DMS",
                "type": names.get(etype, f"0x{etype:02x}"),
                "level": level,
                "flag": {0: "none", 1: "start", 2: "end"}.get(flag, flag),
                "alarm_id": alarm_id,
                "alarm_sn": val[-16:].hex() if len(val) >= 47 else None,  # needed later for 0x9208
            })
        elif eid == 0x01 and elen == 4:
            loc["mileage_km"] = struct.unpack(">I", val)[0] / 10
        elif eid == 0x30 and elen == 1:
            loc["rssi"] = val[0]
        elif eid == 0x31 and elen == 1:
            loc["sats"] = val[0]
    return loc


# ------------------------------------------------------------------ signalling server
SESSIONS = {}  # phone -> Session


class Session:
    def __init__(self, writer):
        self.writer = writer
        self.phone = None
        self.seq = 0
        self.authed = False

    def send(self, msg_id: int, body: bytes = b""):
        self.seq = (self.seq + 1) & 0xFFFF
        self.writer.write(build_808(msg_id, self.phone, self.seq, body))

    def general_reply(self, m, result=0):
        self.send(0x8001, struct.pack(">HHB", m["seq"], m["id"], result))

    def start_live(self, ip: str, tcp_port: int, channel: int, stream: int = 0):
        """0x9101: data type 1 = video only, stream 0 = main / 1 = sub."""
        ipb = ip.encode()
        body = bytes([len(ipb)]) + ipb + struct.pack(">HHBBB", tcp_port, 0, channel, 1, stream)
        self.send(0x9101, body)
        log.info("[%s] 0x9101 live request ch%d -> %s:%d", self.phone, channel, ip, tcp_port)

    def stop_live(self, channel: int):
        self.send(0x9102, bytes([channel, 0, 0, 0]))


async def handle_signal(reader, writer, cfg):
    peer = writer.get_extra_info("peername")
    s = Session(writer)
    buf = b""
    log.info("signal connect %s", peer)
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=cfg.idle_timeout)
            if not chunk:
                break
            buf += chunk
            while True:
                a = buf.find(b"\x7e")
                if a < 0:
                    buf = b""
                    break
                b = buf.find(b"\x7e", a + 1)
                if b < 0:
                    buf = buf[a:]
                    break
                frame, buf = buf[a + 1:b], buf[b + 1:]
                if not frame:  # two 7E back to back
                    buf = b"\x7e" + buf
                    continue
                m = parse_808(frame)
                if m:
                    s.phone = s.phone or m["phone"]
                    on_message(s, m, cfg)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionError) as e:
        log.info("[%s] signal closed: %r", s.phone, e)
    finally:
        if s.phone and SESSIONS.get(s.phone) is s:
            del SESSIONS[s.phone]
        writer.close()


def on_message(s: Session, m: dict, cfg):
    mid, body = m["id"], m["body"]

    if mid == 0x0100:  # register
        maker = body[4:9].decode("ascii", "ignore").strip("\x00 ")
        model = body[9:29].decode("ascii", "ignore").strip("\x00 ")
        term = body[29:36].decode("ascii", "ignore").strip("\x00 ")
        plate = body[37:].decode("gbk", "ignore")
        log.info("[%s] REGISTER maker=%s model=%s term=%s plate=%s", s.phone, maker, model, term, plate)
        auth = cfg.auth_code.encode()
        s.send(0x8100, struct.pack(">HB", m["seq"], 0) + auth)

    elif mid == 0x0102:  # auth
        log.info("[%s] AUTH code=%s", s.phone, body.decode("ascii", "ignore"))
        s.authed = True
        SESSIONS[s.phone] = s
        s.general_reply(m)
        for ch in cfg.channels:
            s.start_live(cfg.public_ip, cfg.media_port, ch, cfg.stream)

    elif mid == 0x0002:  # heartbeat
        s.general_reply(m)

    elif mid == 0x0200:  # location (+ ADAS/DMS alarms)
        loc = parse_location(body)
        log.info("[%s] LOC %.6f,%.6f %.1fkm/h fix=%s acc=%s", s.phone, loc["lat"], loc["lon"],
                 loc["speed_kmh"], loc["fixed"], loc["acc_on"])
        for ev in loc["events"]:
            log.warning("[%s] ALARM %s %s level=%s %s", s.phone, ev["src"], ev["type"], ev["level"], ev["flag"])
        s.general_reply(m)

    elif mid == 0x0704:  # batch location upload (buffered while offline)
        s.general_reply(m)

    elif mid == 0x0001:  # terminal general reply to our command
        rseq, rid, res = struct.unpack(">HHB", body[:5])
        log.info("[%s] device ack 0x%04X result=%d (0=ok,1=fail,2=bad msg,3=unsupported)", s.phone, rid, res)

    elif mid == 0x0003:  # logout
        s.general_reply(m)

    else:
        log.info("[%s] unhandled 0x%04X len=%d sub=%s", s.phone, mid, len(body), m["sub"])
        s.general_reply(m)


# ------------------------------------------------------------------ JT1078 media server
HDR = b"\x30\x31\x63\x64"


def parse_1078(buf: bytes):
    """Returns (packet_dict | None, remaining_buf). Table 19 of JT/T 1078-2016."""
    i = buf.find(HDR)
    if i < 0:
        return None, buf[-3:]
    buf = buf[i:]
    if len(buf) < 16:
        return None, buf
    pt = buf[5] & 0x7F
    sim = buf[8:14].hex()
    ch = buf[14]
    dtype, sub = buf[15] >> 4, buf[15] & 0x0F
    off = 16 + (8 if dtype != 4 else 0) + (4 if dtype <= 2 else 0)
    if len(buf) < off + 2:
        return None, buf
    n = struct.unpack(">H", buf[off:off + 2])[0]
    off += 2
    if len(buf) < off + n:
        return None, buf
    return {"pt": pt, "sim": sim, "ch": ch, "dtype": dtype, "sub": sub, "data": buf[off:off + n]}, buf[off + n:]


class FFmpegSink:
    """Raw H.264/H.265 on stdin -> RTSP publish to MediaMTX."""

    def __init__(self, url: str, codec: str):
        self.url, self.codec, self.proc = url, codec, None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-use_wallclock_as_timestamps", "1",
            "-f", self.codec, "-i", "pipe:0", "-c", "copy",
            "-f", "rtsp", "-rtsp_transport", "tcp", self.url,
            stdin=asyncio.subprocess.PIPE)
        log.info("ffmpeg -> %s (%s)", self.url, self.codec)

    async def write(self, data: bytes):
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.write(data)
            await self.proc.stdin.drain()

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()
            await self.proc.wait()


async def handle_media(reader, writer, cfg):
    peer = writer.get_extra_info("peername")
    log.info("media connect %s", peer)
    buf = b""
    frames = {}  # (sim, ch) -> bytearray being reassembled
    sinks = {}   # (sim, ch) -> FFmpegSink
    got_key = set()
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=cfg.idle_timeout)
            if not chunk:
                break
            buf += chunk
            while True:
                p, buf = parse_1078(buf)
                if p is None:
                    break
                if p["dtype"] > 2:  # skip audio (3) / transparent (4)
                    continue
                key = (p["sim"], p["ch"])
                sub = p["sub"]
                if sub in (0, 1):
                    frames[key] = bytearray(p["data"])
                else:
                    frames.setdefault(key, bytearray()).extend(p["data"])
                if sub not in (0, 2):  # frame not complete yet
                    continue
                frame = bytes(frames.pop(key))

                if key not in got_key:  # start the pipe on the first I-frame
                    if p["dtype"] != 0:
                        continue
                    got_key.add(key)
                    codec = "hevc" if p["pt"] == 99 else "h264"
                    sink = FFmpegSink(f"{cfg.rtsp_base}/{p['sim']}_ch{p['ch']}", codec)
                    await sink.start()
                    sinks[key] = sink
                await sinks[key].write(frame)
    except (asyncio.TimeoutError, ConnectionError, BrokenPipeError) as e:
        log.info("media closed %s: %r", peer, e)
    finally:
        for sink in sinks.values():
            await sink.close()
        writer.close()


# ------------------------------------------------------------------ main
async def main(cfg):
    sig = await asyncio.start_server(lambda r, w: handle_signal(r, w, cfg), "0.0.0.0", cfg.signal_port)
    med = await asyncio.start_server(lambda r, w: handle_media(r, w, cfg), "0.0.0.0", cfg.media_port)
    log.info("JT808 signalling on :%d | JT1078 media on :%d | publish -> %s",
             cfg.signal_port, cfg.media_port, cfg.rtsp_base)
    async with sig, med:
        await asyncio.gather(sig.serve_forever(), med.serve_forever())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--public-ip", required=True, help="IP/domain the dashcam uses to reach MEDIA port")
    ap.add_argument("--signal-port", type=int, default=6608)
    ap.add_argument("--media-port", type=int, default=6609)
    ap.add_argument("--channels", type=int, nargs="+", default=[1, 2], help="1=road, 2=cabin")
    ap.add_argument("--stream", type=int, default=0, help="0=main stream, 1=sub stream")
    ap.add_argument("--rtsp-base", default="rtsp://127.0.0.1:8554")
    ap.add_argument("--auth-code", default="SENTINEL01")
    ap.add_argument("--idle-timeout", type=int, default=180)
    ap.add_argument("-v", "--verbose", action="store_true")
    cfg = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if cfg.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main(cfg))
