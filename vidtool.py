#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vidtool.py - Tra video theo gio dang + phan tich frame de chon screenshot ro net.

Cach dung nhanh:
    python vidtool.py init                          # tao config.json mau
    python vidtool.py find "2026-09-02 21:35"       # tra xem do la video nao
    python vidtool.py analyze /path/video.mp4       # phan tich frame 1 video cu the
    python vidtool.py run "2026-09-02 21:35"        # tra + phan tich luon (top match)

Yeu cau: python3, ffmpeg/ffprobe (tuy chon), pip install opencv-python numpy
"""

import argparse
import base64
import csv
import glob as globmod
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # python < 3.9
    ZoneInfo = None

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "log_paths": ["./logs"],
    "storage_dirs": ["./videos"],
    "timezone": "Asia/Ho_Chi_Minh",
    "tolerance_minutes": 15,
    "video_extensions": [".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".ts"],
    "sample_interval_sec": 0.5,
    "top_n": 12,
    "min_sharpness": 100.0,
    "brightness_range": [40, 225],
    "max_clipped_ratio": 0.35,
    "dedupe_hamming": 10,
    "output_dir": "./output",
    "jpeg_quality": 95,
    "field_map": {"time": None, "file": None, "id": None, "url": None, "title": None},
}

CONFIG_NAME = "config.json"
HERE = os.path.dirname(os.path.abspath(__file__))


def default_config_path():
    """Tim config.json: uu tien thu muc dang dung, khong co thi lay canh script.

    Truoc day chi tra ve "config.json" theo thu muc dang dung, nen chay
    vidtool.py tu cho khac (hoac doi o dia) la mat sach config, roi lang le
    chay bang DEFAULT_CONFIG - khong bao loi nhung khong ra ket qua dung.
    """
    here_cfg = os.path.join(HERE, CONFIG_NAME)
    return CONFIG_NAME if os.path.exists(CONFIG_NAME) else here_cfg


class Cancelled(Exception):
    """Nem ra khi nguoi dung bam DUNG tren giao dien."""


CANCEL_CHECK = None  # gan 1 callable tra ve True de dung giua chung


def _check_cancel():
    try:
        stop = bool(CANCEL_CHECK and CANCEL_CHECK())
    except Exception:
        stop = False
    if stop:
        raise Cancelled("da dung")


def load_config(path=None):
    cfg = dict(DEFAULT_CONFIG)
    path = path or default_config_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if k == "field_map" and isinstance(v, dict):
                fm = dict(cfg["field_map"])
                fm.update(v)
                cfg["field_map"] = fm
            else:
                cfg[k] = v
    return cfg


def get_tz(name):
    if ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    m = re.match(r"^([+-])(\d{2}):?(\d{2})$", str(name))
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
    return timezone(timedelta(hours=7))  # fallback: VN


# ----------------------------------------------------------------------------
# TIME PARSING
# ----------------------------------------------------------------------------

TIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%Y-%m-%d", "%d/%m/%Y", "%Y%m%d_%H%M%S", "%Y%m%d-%H%M%S", "%Y%m%d%H%M%S",
]


def parse_time(value, tz, now=None):
    """Parse gio dang tu nhieu dinh dang -> datetime co timezone. None neu that bai."""
    if value is None:
        return None
    now = now or datetime.now(tz)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _from_epoch(float(value), tz)

    s = str(value).strip()
    if not s:
        return None

    # epoch dang chuoi
    if re.fullmatch(r"\d{10}(\.\d+)?", s):
        return _from_epoch(float(s), tz)
    if re.fullmatch(r"\d{13}", s):
        return _from_epoch(float(s) / 1000.0, tz)

    # ISO co Z / offset
    iso = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt
    except Exception:
        pass

    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue

    # chi co gio "21:35" -> hom nay
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        return now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                           second=int(m.group(3) or 0), microsecond=0)

    # "02/09 21:35" -> nam hien tai
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        try:
            return datetime(now.year, int(m.group(2)), int(m.group(1)),
                            int(m.group(3)), int(m.group(4)),
                            int(m.group(5) or 0), tzinfo=tz)
        except ValueError:
            return None

    try:
        from dateutil import parser as dparser  # optional
        dt = dparser.parse(s, dayfirst=True)
        return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt
    except Exception:
        return None


def _from_epoch(v, tz):
    if v > 1e11:  # milliseconds
        v /= 1000.0
    return datetime.fromtimestamp(v, tz)


# ----------------------------------------------------------------------------
# LOG READING
# ----------------------------------------------------------------------------

TIME_KEYS = ["post_time", "posted_at", "publish_time", "published_at", "upload_time",
             "uploaded_at", "create_time", "created_at", "timestamp", "time", "datetime",
             "date", "gio_dang", "gio", "thoi_gian", "ngay_dang", "thoigian"]
FILE_KEYS = ["local_file", "file_path", "filepath", "file_name", "filename", "file",
             "video_file", "video_path", "source_file", "src", "path", "video",
             "ten_file", "duong_dan", "output", "final_file"]
ID_KEYS = ["video_id", "aweme_id", "item_id", "tiktok_id", "douyin_id", "vid", "id",
           "ma_video", "code"]
URL_KEYS = ["video_url", "tiktok_url", "share_url", "url", "link", "permalink", "duong_link"]
TITLE_KEYS = ["title", "caption", "desc", "description", "content", "text",
              "tieu_de", "noi_dung", "mo_ta"]


def _norm(k):
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def detect_field(keys, candidates, override=None):
    if override:
        return override
    nmap = {_norm(k): k for k in keys}
    for c in candidates:
        if _norm(c) in nmap:
            return nmap[_norm(c)]
    for c in candidates:  # match mem: chua chuoi
        cn = _norm(c)
        for nk, orig in nmap.items():
            if cn and cn in nk:
                return orig
    return None


def expand_paths(paths):
    """Mo rong danh sach duong dan: ~ , va mau glob (* ? [] va ** de quet sau).

    Nho vay config chi can 1 dong cho ca cay thu muc, vi du:
        "E:/Save_Full_Video/**/logs"   -> moi thu muc ten 'logs' o bat ky do sau
        "E:/Save_Full_Video/*/*/logs"  -> chi dung 2 cap
    Duong dan thuong (khong co ky tu dai dien) van giu nguyen nhu cu.
    """
    out, seen = [], set()
    for p in paths or []:
        p = os.path.expanduser(str(p))
        # glob.has_magic khong phai API cong khai -> tu kiem tra
        has_magic = re.search(r"[*?\[]", p) is not None
        hits = sorted(globmod.glob(p, recursive=True)) if has_magic else [p]
        for h in hits:
            key = os.path.normcase(os.path.abspath(h))
            if key not in seen:
                seen.add(key)
                out.append(h)
    return out


def _iter_log_files(paths, exts=(".csv", ".tsv", ".json", ".jsonl", ".ndjson",
                                ".db", ".sqlite", ".sqlite3", ".txt", ".log")):
    for p in expand_paths(paths):
        if os.path.isfile(p):
            yield p
        elif os.path.isdir(p):
            for root, _, files in os.walk(p):
                for fn in sorted(files):
                    if os.path.splitext(fn)[1].lower() in exts:
                        yield os.path.join(root, fn)


def read_log_records(cfg, verbose=False):
    """Doc tat ca log -> list dict thuan (chua parse time)."""
    records = []
    for path in _iter_log_files(cfg["log_paths"]):
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in (".txt", ".log"):
                rows = _read_textlog(path)
                if not rows:
                    rows = _read_delimited(path, None)
            elif ext in (".csv", ".tsv"):
                rows = _read_delimited(path, "\t" if ext == ".tsv" else None)
            elif ext == ".json":
                rows = _read_json(path)
            elif ext in (".jsonl", ".ndjson"):
                rows = _read_jsonl(path)
            elif ext in (".db", ".sqlite", ".sqlite3"):
                rows = _read_sqlite(path)
            else:
                rows = []
        except Exception as e:
            if verbose:
                print("  [!] bo qua %s: %s" % (path, e), file=sys.stderr)
            continue
        for r in rows:
            r["__source_log"] = path
        records.extend(rows)
        if verbose:
            print("  [log] %s -> %d dong" % (path, len(rows)), file=sys.stderr)
    return records


def _read_delimited(path, delim=None):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(8192)
        f.seek(0)
        if delim is None:
            try:
                delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except Exception:
                delim = ","
        out = []
        for r in csv.DictReader(f, delimiter=delim):
            out.append({str(k): v for k, v in r.items() if k is not None})
        return out


LOG_TS_RE = re.compile(
    r"^\s*[\[(]?\s*(?P<ts>\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?)\s*[\])]?\s*(?P<rest>.*)$")
LOG_CHAN_RE = re.compile(r"^\[(?P<chan>[^\]]{1,40})\]\s*(?P<msg>.*)$")
LOG_VIDEO_RE = re.compile(
    r"([^\s\\/:*?\"<>|]+\.(?:mp4|mov|mkv|avi|webm|flv|m4v|ts))", re.I)
LOG_OK_WORDS = ("thanh cong", "thành công", "success", "da dang", "đã đăng",
                "uploaded", "hoan tat", "hoàn tất", "done")


def _read_textlog(path):
    """Log text thuan: moi dong co gio + ten file video."""
    out = []
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            m = LOG_TS_RE.match(line.rstrip())
            if not m:
                continue
            rest = m.group("rest")
            chan = ""
            c = LOG_CHAN_RE.match(rest)
            if c:
                chan, rest = c.group("chan"), c.group("msg")
            v = LOG_VIDEO_RE.search(rest)
            if not v:
                continue
            low = rest.lower()
            out.append({
                "post_time": m.group("ts"),
                "local_file": v.group(1),
                "channel": chan,
                "event": rest.strip()[:160],
                "success": any(w in low for w in LOG_OK_WORDS),
            })
    return out


def _read_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def _read_jsonl(path):
    out = []
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if isinstance(d, dict):
                    out.append(d)
            except Exception:
                continue
    return out


def _read_sqlite(path):
    out = []
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            try:
                for row in con.execute('SELECT * FROM "%s" LIMIT 200000' % t):
                    d = dict(row)
                    d["__table"] = t
                    out.append(d)
            except Exception:
                continue
    finally:
        con.close()
    return out


# ----------------------------------------------------------------------------
# TIM VIDEO
# ----------------------------------------------------------------------------

def index_storage(cfg):
    """Quet thu muc luu tru -> list (path, mtime, size)."""
    exts = set(e.lower() for e in cfg["video_extensions"])
    files = []
    for d in expand_paths(cfg["storage_dirs"]):
        if os.path.isfile(d):
            files.append(d)
            continue
        for root, _, fns in os.walk(d):
            for fn in fns:
                if os.path.splitext(fn)[1].lower() in exts:
                    files.append(os.path.join(root, fn))
    out = []
    for p in files:
        try:
            st = os.stat(p)
            out.append({"path": p, "mtime": st.st_mtime, "size": st.st_size})
        except OSError:
            continue
    return out


def resolve_file(record, fields, storage, cfg):
    """Tu 1 dong log -> tim file thuc te tren dia. Tra (path, ly_do)."""
    by_base = {}
    by_stem = {}
    for s in storage:
        by_base.setdefault(os.path.basename(s["path"]).lower(), []).append(s["path"])
        by_stem.setdefault(os.path.splitext(os.path.basename(s["path"]))[0].lower(), []).append(s["path"])

    # 1. cot file trong log
    fname = record.get(fields["file"]) if fields["file"] else None
    if fname:
        fname = str(fname).strip().strip('"')
        if os.path.isfile(fname):
            return fname, "duong dan tuyet doi trong log"
        base = os.path.basename(fname.replace("\\", "/")).lower()
        if base in by_base:
            return by_base[base][0], "khop ten file trong log"
        stem = os.path.splitext(base)[0].lower()
        if stem in by_stem:
            return by_stem[stem][0], "khop ten file (khac duoi) trong log"
        for s in storage:  # khop mem
            if stem and stem in os.path.basename(s["path"]).lower():
                return s["path"], "ten file trong log nam trong ten file luu tru"

    # 2. video id trong ten file
    vid = record.get(fields["id"]) if fields["id"] else None
    if vid:
        vid = str(vid).strip()
        if len(vid) >= 6:
            for s in storage:
                if vid.lower() in os.path.basename(s["path"]).lower():
                    return s["path"], "video id nam trong ten file"

    # 3. thoi gian file gan nhat
    dt = record.get("__dt")
    if dt and storage:
        target = dt.timestamp()
        tol = cfg["tolerance_minutes"] * 60 * 4
        best = min(storage, key=lambda s: abs(s["mtime"] - target))
        if abs(best["mtime"] - target) <= tol:
            return best["path"], "mtime gan gio dang (lech %.1f phut)" % (
                abs(best["mtime"] - target) / 60.0)
    return None, "khong tim thay file tren dia"


def find_candidates(query_time, cfg, verbose=False):
    tz = get_tz(cfg["timezone"])
    qdt = parse_time(query_time, tz)
    if qdt is None:
        raise SystemExit("Khong doc duoc gio '%s'. Thu dang: 2026-09-02 21:35" % query_time)

    records = read_log_records(cfg, verbose=verbose)
    storage = index_storage(cfg)
    if verbose:
        print("  [storage] %d file video" % len(storage), file=sys.stderr)

    # moi file log co the co schema khac nhau -> nhan dien cot theo tung bo key
    fm = cfg["field_map"]
    cache = {}

    def fields_for(rec):
        sig = tuple(sorted(str(k) for k in rec.keys()))
        if sig not in cache:
            keys = set(sig)
            cache[sig] = {
                "time": detect_field(keys, TIME_KEYS, fm.get("time")),
                "file": detect_field(keys, FILE_KEYS, fm.get("file")),
                "id": detect_field(keys, ID_KEYS, fm.get("id")),
                "url": detect_field(keys, URL_KEYS, fm.get("url")),
                "title": detect_field(keys, TITLE_KEYS, fm.get("title")),
            }
            if verbose:
                print("  [fields] %s -> %s" % (rec.get("__source_log"), cache[sig]),
                      file=sys.stderr)
        return cache[sig]

    tol = timedelta(minutes=cfg["tolerance_minutes"])
    cands = []
    fields = {"time": None, "file": None, "id": None, "url": None, "title": None}

    for r in records:
        f = fields_for(r)
        if not f["time"]:
            continue
        fields = f
        dt = parse_time(r.get(f["time"]), tz)
        if dt is None:
            continue
        delta = abs((dt - qdt).total_seconds())
        if delta <= tol.total_seconds():
            r["__dt"] = dt
            path, why = resolve_file(r, f, storage, cfg)
            cands.append({
                "delta_sec": delta, "dt": dt, "path": path, "why": why,
                "id": r.get(f["id"]) if f["id"] else None,
                "url": r.get(f["url"]) if f["url"] else None,
                "title": r.get(f["title"]) if f["title"] else None,
                "log": r.get("__source_log"), "source": "log",
                "success": bool(r.get("success")),
                "event": r.get("event"),
            })

    # fallback: khong co log khop -> quet mtime
    if not cands:
        target = qdt.timestamp()
        wide = cfg["tolerance_minutes"] * 60 * 6
        for s in storage:
            delta = abs(s["mtime"] - target)
            if delta <= wide:
                cands.append({
                    "delta_sec": delta, "dt": datetime.fromtimestamp(s["mtime"], tz),
                    "path": s["path"], "why": "chi khop mtime (khong thay trong log)",
                    "id": None, "url": None, "title": None, "log": None,
                    "source": "mtime",
                })

    grouped, loose = {}, []
    for c in cands:
        if not c["path"]:
            loose.append(c)
            continue
        key = os.path.normcase(os.path.abspath(c["path"]))
        rank = (0 if c.get("success") else 1, c["delta_sec"])
        g = grouped.get(key)
        if g is None:
            c["hits"] = 1
            grouped[key] = (rank, c)
        else:
            hits = g[1].get("hits", 1) + 1
            if rank < g[0]:
                c["hits"] = hits
                grouped[key] = (rank, c)
            else:
                g[1]["hits"] = hits
    cands = [g[1] for g in grouped.values()] + loose
    cands.sort(key=lambda c: (c["path"] is None,
                              0 if c.get("success") else 1, c["delta_sec"]))
    return qdt, cands, fields


# ----------------------------------------------------------------------------
# PHAN TICH FRAME
# ----------------------------------------------------------------------------

def _lazy_cv():
    try:
        import cv2
        import numpy as np
        return cv2, np
    except ImportError:
        raise SystemExit("Can opencv: pip install opencv-python numpy")


def ffprobe_info(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout) if out.returncode == 0 else {}
    except Exception:
        return {}


def dhash(gray, cv2, size=8):
    small = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA)
    diff = small[:, 1:] > small[:, :-1]
    bits = 0
    for i, v in enumerate(diff.flatten()):
        if v:
            bits |= (1 << i)
    return bits


def hamming(a, b):
    return bin(a ^ b).count("1")


def colorfulness(bgr, np):
    b, g, r = bgr[:, :, 0].astype("float"), bgr[:, :, 1].astype("float"), bgr[:, :, 2].astype("float")
    rg, yb = np.abs(r - g), np.abs(0.5 * (r + g) - b)
    return float(np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


def measure_frames(path, cfg, verbose=False):
    """Pass 1: quet video, do chi so tung frame mau."""
    cv2, np = _lazy_cv()
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit("Khong mo duoc video: %s" % path)

    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    if not fps or fps != fps or fps > 240:
        fps = 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, int(round(fps * float(cfg["sample_interval_sec"]))))

    face_cascade = None
    try:
        xml = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        if os.path.exists(xml):
            face_cascade = cv2.CascadeClassifier(xml)
    except Exception:
        pass

    frames = []
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step == 0:
            try:
                _check_cancel()
            except Cancelled:
                cap.release()
                raise
            ok, frame = cap.retrieve()
            if ok and frame is not None:
                frames.append(_measure_one(frame, idx, idx / fps, cv2, np, face_cascade, cfg))
        idx += 1
    cap.release()

    if verbose:
        print("  [analyze] %d frame mau tu %d frame, fps=%.2f" % (len(frames), total or idx, fps),
              file=sys.stderr)
    return frames, fps, total or idx


def _measure_one(frame, index, t_sec, cv2, np, face_cascade, cfg):
    h, w = frame.shape[:2]
    scale = 720.0 / max(w, 1)
    work = cv2.resize(frame, (720, max(1, int(h * scale)))) if scale < 1 else frame
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)

    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    tenengrad = float(np.mean(cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3) ** 2 +
                              cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3) ** 2))
    brightness = float(gray.mean())
    contrast = float(gray.std())
    clipped = float(((gray <= 4) | (gray >= 251)).mean())
    color = colorfulness(work, np)

    faces, face_area = 0, 0.0
    if face_cascade is not None:
        try:
            det = face_cascade.detectMultiScale(gray, 1.15, 5, minSize=(40, 40))
            faces = len(det)
            if faces:
                fw, fh = max(det, key=lambda d: d[2] * d[3])[2:4]
                face_area = 100.0 * (fw * fh) / float(gray.shape[0] * gray.shape[1])
        except Exception:
            pass

    thumb = cv2.resize(work, (240, max(1, int(work.shape[0] * 240.0 / work.shape[1]))))
    ok, buf = cv2.imencode(".jpg", thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 70])

    return {
        "index": index, "time_s": round(t_sec, 3), "width": w, "height": h,
        "sharpness": round(sharp, 2), "tenengrad": round(tenengrad, 2),
        "brightness": round(brightness, 2), "contrast": round(contrast, 2),
        "clipped_ratio": round(clipped, 4), "colorfulness": round(color, 2),
        "faces": faces, "face_area_pct": round(face_area, 2),
        "hash": dhash(gray, cv2),
        "thumb": base64.b64encode(buf.tobytes()).decode() if ok else "",
    }


def score_and_filter(frames, cfg):
    if not frames:
        return []
    lo, hi = cfg["brightness_range"]

    def prank(key):
        vals = sorted(f[key] for f in frames)
        n = len(vals)
        def rank(v):
            import bisect
            return bisect.bisect_left(vals, v) / max(1, n - 1)
        return rank

    r_sharp, r_ten, r_con, r_col = (prank("sharpness"), prank("tenengrad"),
                                    prank("contrast"), prank("colorfulness"))

    for f in frames:
        expo = 1.0
        if f["brightness"] < lo or f["brightness"] > hi:
            expo = 0.2
        expo *= max(0.0, 1.0 - f["clipped_ratio"] / max(1e-6, cfg["max_clipped_ratio"]))
        face_bonus = 1.0 if f["faces"] and f["face_area_pct"] >= 1.0 else 0.0
        f["score"] = round(100.0 * (
            0.38 * r_sharp(f["sharpness"]) + 0.12 * r_ten(f["tenengrad"]) +
            0.15 * r_con(f["contrast"]) + 0.08 * r_col(f["colorfulness"]) +
            0.15 * expo + 0.12 * face_bonus), 2)
        f["usable"] = bool(
            f["sharpness"] >= cfg["min_sharpness"] and
            lo <= f["brightness"] <= hi and
            f["clipped_ratio"] <= cfg["max_clipped_ratio"] and
            f["contrast"] >= 15.0)

    # dedupe: giu frame diem cao nhat trong nhom giong nhau
    kept = []
    for f in sorted(frames, key=lambda x: -x["score"]):
        dup_of = None
        for k in kept:
            if hamming(f["hash"], k["hash"]) <= cfg["dedupe_hamming"]:
                dup_of = k["index"]
                break
        f["dup_of"] = dup_of
        if dup_of is None:
            kept.append(f)
    return frames


def extract_frames(path, indices, outdir, cfg, prefix="best"):
    """Pass 2: lay lai dung frame da chon o do phan giai goc."""
    cv2, _ = _lazy_cv()
    os.makedirs(outdir, exist_ok=True)
    want = {int(i): rank for rank, i in enumerate(indices, 1)}
    saved = {}
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    idx = 0
    remaining = set(want)
    while remaining:
        ok = cap.grab()
        if not ok:
            break
        if idx in remaining:
            try:
                _check_cancel()
            except Cancelled:
                cap.release()
                raise
            ok, frame = cap.retrieve()
            if ok and frame is not None:
                fn = "%s_%02d_t%07.2fs.jpg" % (prefix, want[idx], idx / max(fps, 1e-6))
                fp = os.path.join(outdir, fn)
                cv2.imwrite(fp, frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(cfg["jpeg_quality"])])
                saved[idx] = fp
            remaining.discard(idx)
        idx += 1
    cap.release()
    return saved


CSV_COLS = ["rank", "index", "time_s", "score", "usable", "dup_of", "sharpness",
            "tenengrad", "brightness", "contrast", "clipped_ratio", "colorfulness",
            "faces", "face_area_pct", "width", "height", "saved_file"]


def write_report(video, frames, picks, saved, outdir, info):
    os.makedirs(outdir, exist_ok=True)
    csv_path = os.path.join(outdir, "frames.csv")
    ranked = sorted(frames, key=lambda f: -f["score"])
    rank_of = {f["index"]: i + 1 for i, f in enumerate(ranked)}
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for fr in ranked:
            row = dict(fr)
            row["rank"] = rank_of[fr["index"]]
            row["saved_file"] = saved.get(fr["index"], "")
            w.writerow(row)

    html_path = os.path.join(outdir, "contact_sheet.html")
    cards = []
    for i, fr in enumerate(picks, 1):
        fp = saved.get(fr["index"], "")
        if fp and os.path.isfile(fp):
            src = os.path.basename(fp)          # anh full do phan giai, cung thu muc
            href = src
        else:
            src = "data:image/jpeg;base64,%s" % fr["thumb"]
            href = ""
        cap = ('<figcaption><b>#%d &middot; %.2fs</b> &middot; %dx%d<br>score %.1f<br>'
               'net %.0f &middot; sang %.0f &middot; tuong phan %.0f<br>mat: %d</figcaption>'
               % (i, fr["time_s"], fr.get("width", 0), fr.get("height", 0), fr["score"],
                  fr["sharpness"], fr["brightness"], fr["contrast"], fr["faces"]))
        img = '<img src="%s" loading="lazy" alt="frame %d">' % (src, fr["index"])
        if href:
            img = '<a href="%s" target="_blank">%s</a>' % (href, img)
        cards.append("<figure>%s%s</figure>" % (img, cap))
    usable = sum(1 for f in frames if f["usable"] and f["dup_of"] is None)
    dur = ""
    try:
        dur = "%.1fs" % float(info.get("format", {}).get("duration", 0))
    except Exception:
        pass
    html = """<!doctype html><meta charset="utf-8"><title>Frames - %s</title>
<style>body{font-family:system-ui,sans-serif;margin:24px;background:#111;color:#eee}
h1{font-size:18px}p{color:#aaa;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(460px,1fr));gap:16px}
figure{margin:0;background:#1c1c1c;border-radius:8px;overflow:hidden}
img{width:100%%;display:block}figcaption{padding:8px;font-size:12px;line-height:1.5;color:#bbb}
</style><h1>%s</h1>
<p>Thoi luong %s &middot; %d frame da quet &middot; <b>%d frame ro rang khong trung lap</b>
&middot; hien %d frame tot nhat &middot; <i>anh duoi la file JPG goc, bam vao de mo full</i></p><div class="grid">%s</div>""" % (
        os.path.basename(video), os.path.basename(video), dur, len(frames),
        usable, len(picks), "".join(cards))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    return csv_path, html_path, usable


def analyze_video(path, cfg, verbose=False, outdir=None):
    if not os.path.isfile(path):
        raise SystemExit("Khong thay file: %s" % path)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.splitext(os.path.basename(path))[0])[:60]
    outdir = outdir or os.path.join(cfg["output_dir"], stem)
    info = ffprobe_info(path)

    frames, fps, total = measure_frames(path, cfg, verbose=verbose)
    score_and_filter(frames, cfg)

    good = [f for f in frames if f["usable"] and f["dup_of"] is None]
    good.sort(key=lambda f: -f["score"])
    picks = good[: int(cfg["top_n"])]
    saved = extract_frames(path, [f["index"] for f in picks], outdir, cfg) if picks else {}
    csv_path, html_path, usable = write_report(path, frames, picks, saved, outdir, info)

    return {
        "video": path, "outdir": outdir, "frames_scanned": len(frames),
        "usable_unique": usable, "saved": len(saved),
        "csv": csv_path, "html": html_path, "fps": round(fps, 2), "total_frames": total,
        "picks": [{"index": f["index"], "time_s": f["time_s"], "score": f["score"],
                   "sharpness": f["sharpness"], "faces": f["faces"],
                   "file": saved.get(f["index"], "")} for f in picks],
    }


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def cmd_init(args):
    path = args.config or CONFIG_NAME
    if os.path.exists(path) and not args.force:
        print("Da co %s (dung --force de ghi de)" % path)
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
    print("Da tao %s - sua log_paths va storage_dirs cho dung may cua ban." % path)


def _print_candidates(qdt, cands, limit=10):
    print("Gio tra: %s" % qdt.strftime("%Y-%m-%d %H:%M:%S %Z"))
    if not cands:
        print("Khong co ung vien nao. Kiem tra log_paths / storage_dirs / tolerance_minutes.")
        return
    print("%d ung vien (hien %d):\n" % (len(cands), min(limit, len(cands))))
    for i, c in enumerate(cands[:limit], 1):
        print("%2d. lech %6.1f phut | %s" % (i, c["delta_sec"] / 60.0,
                                             c["dt"].strftime("%Y-%m-%d %H:%M:%S")))
        print("    file : %s" % (c["path"] or "(khong tim thay)"))
        print("    vi   : %s" % c["why"])
        if c.get("event"):
            print("    log  : %s%s" % ("[OK] " if c.get("success") else "", c["event"]))
        if c.get("hits", 1) > 1:
            print("    (%d dong log cung tro toi file nay)" % c["hits"])
        if c["id"]:
            print("    id   : %s" % c["id"])
        if c["title"]:
            print("    tieu de: %s" % str(c["title"])[:100])
        if c["url"]:
            print("    url  : %s" % c["url"])
        print()


def cmd_find(args):
    cfg = load_config(args.config)
    if args.tolerance:
        cfg["tolerance_minutes"] = args.tolerance
    qdt, cands, _ = find_candidates(args.time, cfg, verbose=args.verbose)
    if args.json:
        print(json.dumps({"query": qdt.isoformat(), "candidates": [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in c.items()}
            for c in cands]}, ensure_ascii=False, indent=2))
    else:
        _print_candidates(qdt, cands, args.limit)


def cmd_analyze(args):
    cfg = load_config(args.config)
    if args.top:
        cfg["top_n"] = args.top
    if args.interval:
        cfg["sample_interval_sec"] = args.interval
    res = analyze_video(args.video, cfg, verbose=args.verbose, outdir=args.out)
    _print_analysis(res, args.json)


def _print_analysis(res, as_json=False):
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    print("\nVideo   : %s" % res["video"])
    print("Da quet : %d frame mau (fps %.2f, tong %d frame)"
          % (res["frames_scanned"], res["fps"], res["total_frames"]))
    print("KET QUA : %d frame ro rang, khong trung lap" % res["usable_unique"])
    print("Da luu  : %d anh -> %s" % (res["saved"], res["outdir"]))
    print("Bang do : %s" % res["csv"])
    print("Xem luoi: %s" % res["html"])
    if res["picks"]:
        print("\nTop frame:")
        print("  %-4s %-9s %-7s %-9s %s" % ("#", "giay", "diem", "do net", "file"))
        for i, p in enumerate(res["picks"], 1):
            print("  %-4d %-9.2f %-7.1f %-9.0f %s"
                  % (i, p["time_s"], p["score"], p["sharpness"],
                     os.path.basename(p["file"])))


def cmd_run(args):
    cfg = load_config(args.config)
    if args.tolerance:
        cfg["tolerance_minutes"] = args.tolerance
    if args.top:
        cfg["top_n"] = args.top
    if args.interval:
        cfg["sample_interval_sec"] = args.interval
    qdt, cands, _ = find_candidates(args.time, cfg, verbose=args.verbose)
    _print_candidates(qdt, cands, args.limit)
    found = [c for c in cands if c["path"]]
    if not found:
        raise SystemExit("Khong co file nao de phan tich.")
    targets = found if args.all else found[:1]
    for c in targets:
        print("=" * 70)
        print("Phan tich: %s" % c["path"])
        _print_analysis(analyze_video(c["path"], cfg, verbose=args.verbose), args.json)


def main():
    ap = argparse.ArgumentParser(
        description="Tra video theo gio dang + phan tich frame ro net.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("-c", "--config", help="duong dan config.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--json", action="store_true", help="xuat JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="tao config.json mau")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("find", help="tra video tu gio dang")
    p.add_argument("time", help='vd "2026-09-02 21:35" hoac "21:35"')
    p.add_argument("--tolerance", type=float, help="sai so phut")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("analyze", help="phan tich frame 1 video")
    p.add_argument("video")
    p.add_argument("--top", type=int, help="so anh luu")
    p.add_argument("--interval", type=float, help="giay giua 2 frame mau")
    p.add_argument("--out", help="thu muc xuat")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("run", help="tra roi phan tich luon")
    p.add_argument("time")
    p.add_argument("--tolerance", type=float)
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--top", type=int)
    p.add_argument("--interval", type=float)
    p.add_argument("--all", action="store_true", help="phan tich moi ung vien")
    p.set_defaults(func=cmd_run)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()