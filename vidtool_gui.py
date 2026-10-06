#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vidtool_gui.py - Giao dien cho vidtool.py
Chay: python vidtool_gui.py   (hoac bam dup GIAO DIEN.bat)
"""

import contextlib
import ctypes
import glob
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

def app_dir():
    """Thu muc dat app, de con tim config.json va thu muc output.

    Khi dong goi bang PyInstaller onefile, __file__ tro vao thu muc tam
    (sys._MEIPASS) bi xoa sau khi thoat - ghi config vao do la mat sach.
    Luc do phai lay thu muc chua file .exe.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


if getattr(sys, "frozen", False):
    # app dong goi che do --windowed khong co console: sys.stdout/stderr la None,
    # moi cau print() ngoai vung redirect se nem AttributeError.
    for _name in ("stdout", "stderr"):
        if getattr(sys, _name, None) is None:
            setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))

HERE = app_dir()
# Thu muc chua chinh file nay. Phai dung truoc ban dong goi trong sys.path:
# updater tai vidtool.py moi ve day, lay nham ban cu trong .exe la cap nhat
# code mot dang nhung chay mot neo.
CODE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CODE_DIR)
_BUNDLED = getattr(sys, "_MEIPASS", None)
if _BUNDLED and _BUNDLED not in sys.path:
    sys.path.append(_BUNDLED)      # chi dung lam phao cuu sinh

import vidtool  # noqa: E402

CONFIG_PATH = os.path.join(HERE, "config.json")

BG = "#f0f0f0"
LOG_BG = "#111111"
LOG_FG = "#e6e6e6"
BTN_PRIMARY = "#cfe8ff"
BTN_DANGER = "#ffd6d6"

DONE = "\x00__DONE__\x00"            # worker phan tich bao da xong
FLOW_DONE = "\x00__FLOW_DONE__\x00"  # worker dan anh vao Flow bao da xong

FLOW_URL = "https://labs.google/fx/vi/tools/flow"

# Cho trang Flow load xong roi moi them anh (giay)
FLOW_DELAY = 8.0
# Doi hop thoai chon file hien ra toi da bao lau (giay)
FLOW_DIALOG_WAIT = 90.0
# Vi tri nut "+" ("Them noi dung nghe nhin") tren thanh dau trang Flow.
# Thanh dau trang cua Flow dinh vao goc TREN - PHAI, nen phai neo theo pixel tinh
# tu goc do chu khong phai ti le be ngang cua so: doi be rong cua so hay bat/tat
# DevTools la ti le sai ngay, con khoang cach toi mep phai thi khong doi.
# (cach mep phai, cach mep tren) - bam "Do lai" de ghi cho dung may cua ban.
FLOW_PLUS_ANCHOR = (242, 148)
# Nut "+" mo ra mot menu. Muc "Tai noi dung nghe nhin len" nam ngay duoi nut,
# lech bao nhieu pixel so voi diem vua click (ngang, doc).
FLOW_UPLOAD_OFFSET = (-69, 36)
# Cho menu hien ra roi moi click vao muc tai len (giay)
FLOW_MENU_WAIT = 1.0
# Vi tri o nhap cua Flow - dung cho che do dan Ctrl+V
FLOW_INPUT = (0.50, 0.915)

# Cach dua anh vao Flow
MODE_DIALOG = "dialog"   # click "+" roi tu dien vao hop thoai chon file cua Windows
MODE_PASTE = "paste"     # copy anh len clipboard roi Ctrl+V (Flow khong nhan)
MODE_OFF = "off"         # chi mo Flow, khong dung toi chuot/ban phim
MODE_LABELS = [("Hop thoai chon file (Flow nhan kieu nay)", MODE_DIALOG),
               ("Dan Ctrl+V", MODE_PASTE),
               ("Chi mo Flow, khong tu them", MODE_OFF)]

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---------------------------------------------------------------------------
# Clipboard + dieu khien cua so (chi Windows, dung ctypes - khong can cai them)
# ---------------------------------------------------------------------------

def _ratio(value, fallback):
    """Doc cap ti le [ngang, doc] tu config, sai thi lay mac dinh."""
    try:
        return (float(value[0]), float(value[1]))
    except Exception:
        return fallback


def _best_jpgs(folder):
    """Danh sach anh best_*.jpg trong 1 thu muc, sap theo thu hang."""
    return sorted(glob.glob(os.path.join(glob.escape(folder), "best_*.jpg")))


def copy_files_to_clipboard(paths):
    """Dat danh sach file len clipboard giong nhu Copy trong Explorer (CF_HDROP).

    Dung Set-Clipboard cua PowerShell de khoi phai them thu vien.
    Tra ve (True, "") neu xong, (False, ly_do) neu that bai.
    """
    if os.name != "nt":
        return False, "chi ho tro Windows"
    if not paths:
        return False, "khong co file nao"
    quoted = ",".join("'%s'" % p.replace("'", "''") for p in paths)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Set-Clipboard -LiteralPath %s" % quoted],
            capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
    except Exception as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or "").strip()[:300] or "Set-Clipboard loi"
    return True, ""


def _user32():
    """user32 da khai bao kieu tra ve. Khong khai bao thi HWND bi cat con 32 bit."""
    if os.name != "nt":
        raise OSError("chi ho tro Windows")
    from ctypes import wintypes
    u = ctypes.windll.user32
    if not getattr(u, "_vidtool_ready", False):
        u.SetProcessDPIAware()
        for name in ("GetForegroundWindow", "FindWindowExW", "SetFocus",
                     "WindowFromPoint", "GetAncestor"):
            getattr(u, name).restype = wintypes.HWND
        u.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND,
                                    wintypes.LPCWSTR, wintypes.LPCWSTR]
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        u.SendMessageW.restype = ctypes.c_ssize_t
        u.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                   ctypes.c_size_t, ctypes.c_wchar_p]
        u._vidtool_ready = True
    return u


def find_chrome_window(prefer=("flow", "labs.google"), size_hint=None):
    """Tim cua so Chrome dang mo Flow.

    Hai cai bay o day:
      - Tab Flow lay ten du an lam tieu de (vi du "02:16 04 thg 9"), khong chac
        co chu "flow", nen khong the chi doi chieu tieu de.
      - Co the co NHIEU cua so Chrome cung mo Flow, tieu de giong het nhau.
        Luc do phai phan biet bang kich thuoc khung da ghi khi do vi tri.

    Thu tu uu tien: khop tu khoa + dung kich thuoc -> khop tu khoa -> dung
    kich thuoc -> cua so dang o truoc -> cua so Chrome dau tien.
    EnumWindows tra ve theo thu tu tren cung xuong, nen cai dau danh sach la
    cua so vua duoc Chrome dua len khi mo URL.
    """
    from ctypes import wintypes
    u = _user32()
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        if _class_of(hwnd) == "Chrome_WidgetWin_1":
            found.append((hwnd, buf.value))
        return True

    u.EnumWindows(cb, 0)
    if not found:
        return None, ""

    keys = (prefer,) if isinstance(prefer, str) else tuple(prefer or ())

    def named(item):
        return any(k and k in item[1].lower() for k in keys)

    def sized(item):
        return bool(size_hint) and window_size(item[0]) == tuple(size_hint)

    for test in (lambda it: named(it) and sized(it), named, sized):
        for item in found:
            if test(item):
                return item

    fg = u.GetForegroundWindow()
    for hwnd, title in found:
        if hwnd == fg:
            return hwnd, title
    return found[0]


def chrome_window_at_cursor():
    """Cua so Chrome dang nam duoi con tro chuot.

    Dung khi do vi tri: luc do giao dien vidtool moi la cua so dang o truoc,
    Chrome chi dang nam duoi chuot chu chua duoc click.
    """
    from ctypes import wintypes
    u = _user32()
    pt = wintypes.POINT()
    u.GetCursorPos(ctypes.byref(pt))
    hwnd = u.WindowFromPoint(pt)
    if not hwnd:
        return None, ""
    root = u.GetAncestor(hwnd, 2)   # GA_ROOT
    if root:
        hwnd = root
    if _class_of(hwnd) != "Chrome_WidgetWin_1":
        return None, ""
    return hwnd, _title_of(hwnd)


def window_size(hwnd):
    """(rong, cao) vung noi dung cua so, tinh bang pixel."""
    from ctypes import wintypes
    u = _user32()
    rect = wintypes.RECT()
    u.GetClientRect(hwnd, ctypes.byref(rect))
    return rect.right, rect.bottom


def focus_window(hwnd):
    """Dua cua so len truoc. Windows chan SetForegroundWindow nen phai muon thread."""
    u = _user32()
    k = ctypes.windll.kernel32
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
    fg = u.GetForegroundWindow()
    me = k.GetCurrentThreadId()
    tids = set()
    for h in (fg, hwnd):
        if h:
            t = u.GetWindowThreadProcessId(h, None)
            if t and t != me:
                tids.add(t)
    for t in tids:
        u.AttachThreadInput(me, t, True)
    try:
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
    finally:
        for t in tids:
            u.AttachThreadInput(me, t, False)
    time.sleep(0.25)
    return u.GetForegroundWindow() == hwnd


def click_at(x, y):
    """Click chuot trai vao mot toa do man hinh."""
    u = _user32()
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.2)
    u.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
    time.sleep(0.06)
    u.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP
    time.sleep(0.25)
    return int(x), int(y)


def client_point(hwnd, rx, ry):
    """Doi ti le khung cua so thanh toa do man hinh."""
    from ctypes import wintypes
    u = _user32()
    rect = wintypes.RECT()
    u.GetClientRect(hwnd, ctypes.byref(rect))
    pt = wintypes.POINT(int(rect.right * rx), int(rect.bottom * ry))
    u.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def click_in_window(hwnd, rx, ry):
    """Click vao mot diem trong cua so, toa do tinh theo ti le khung."""
    return click_at(*client_point(hwnd, rx, ry))


def anchor_point(hwnd, from_right, from_top):
    """Doi neo (cach mep phai, cach mep tren) thanh toa do man hinh."""
    from ctypes import wintypes
    u = _user32()
    rect = wintypes.RECT()
    u.GetClientRect(hwnd, ctypes.byref(rect))
    pt = wintypes.POINT(int(rect.right - from_right), int(from_top))
    u.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def cursor_anchor_in_window(hwnd):
    """Vi tri chuot quy ve neo (cach mep phai, cach mep tren). None neu chuot o ngoai."""
    from ctypes import wintypes
    u = _user32()
    pt = wintypes.POINT()
    u.GetCursorPos(ctypes.byref(pt))
    rect = wintypes.RECT()
    u.GetClientRect(hwnd, ctypes.byref(rect))
    origin = wintypes.POINT(0, 0)
    u.ClientToScreen(hwnd, ctypes.byref(origin))
    w, h = rect.right, rect.bottom
    if w <= 0 or h <= 0:
        return None
    cx, cy = pt.x - origin.x, pt.y - origin.y
    if not (0 <= cx <= w and 0 <= cy <= h):
        return None
    return w - cx, cy


def send_ctrl_v():
    u = _user32()
    u.keybd_event(0x11, 0, 0, 0)        # Ctrl down
    time.sleep(0.04)
    u.keybd_event(0x56, 0, 0, 0)        # V down
    time.sleep(0.04)
    u.keybd_event(0x56, 0, 2, 0)        # V up
    time.sleep(0.02)
    u.keybd_event(0x11, 0, 2, 0)        # Ctrl up


def send_enter():
    u = _user32()
    u.keybd_event(0x0D, 0, 0, 0)
    time.sleep(0.04)
    u.keybd_event(0x0D, 0, 2, 0)


def _class_of(hwnd):
    u = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _title_of(hwnd):
    u = ctypes.windll.user32
    n = u.GetWindowTextLengthW(hwnd)
    if not n:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def find_file_dialog():
    """Tim hop thoai chon file cua Windows (lop #32770) dang hien."""
    from ctypes import wintypes
    u = _user32()
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        if u.IsWindowVisible(hwnd) and _class_of(hwnd) == "#32770":
            hits.append(hwnd)
        return True

    u.EnumWindows(cb, 0)
    # hop thoai chon file luon co o nhap ten file -> dung no de loai tru dialog khac
    for hwnd in hits:
        if find_filename_edit(hwnd):
            return hwnd
    return None


def find_filename_edit(dlg):
    """O nhap 'Ten file' cua hop thoai: #32770 > ComboBoxEx32 > ComboBox > Edit.

    Phai bam dung chuoi lop nay, khong duoc lay o Edit dau tien gap duoc:
    hop thoai con co o tim kiem va thanh dia chi cung la lop Edit.
    """
    u = _user32()
    cbex = u.FindWindowExW(dlg, None, "ComboBoxEx32", None)
    if cbex:
        combo = u.FindWindowExW(cbex, None, "ComboBox", None)
        if combo:
            edit = u.FindWindowExW(combo, None, "Edit", None)
            if edit:
                return edit
    # vai hop thoai bo lop ComboBox, o Edit nam thang duoi dialog
    return u.FindWindowExW(dlg, None, "Edit", None) or None


def set_window_text(hwnd, text):
    u = _user32()
    u.SendMessageW(hwnd, 0x000C, 0, ctypes.c_wchar_p(text))  # WM_SETTEXT


def fill_file_dialog(dlg, paths):
    """Go duong dan vao o ten file roi bam Enter. Nhieu file thi boc trong nhay kep."""
    u = _user32()
    edit = find_filename_edit(dlg)
    if not edit:
        return False, "khong thay o nhap ten file trong hop thoai"
    focus_window(dlg)
    value = " ".join('"%s"' % p for p in paths) if len(paths) > 1 else paths[0]
    set_window_text(edit, value)
    time.sleep(0.2)
    u.SetFocus(edit)
    send_enter()
    return True, ""


# ---------------------------------------------------------------------------
# Chrome ca nhan
# ---------------------------------------------------------------------------

def chrome_exe():
    """Tim chrome.exe o cac vi tri cai dat thong thuong."""
    roots = [os.environ.get("PROGRAMFILES", r"C:\Program Files"),
             os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
             os.environ.get("LOCALAPPDATA", "")]
    for r in roots:
        if not r:
            continue
        p = os.path.join(r, "Google", "Chrome", "Application", "chrome.exe")
        if os.path.isfile(p):
            return p
    return ""


def chrome_userdata():
    la = os.environ.get("LOCALAPPDATA", "")
    d = os.path.join(la, "Google", "Chrome", "User Data") if la else ""
    return d if os.path.isdir(d) else ""


def chrome_profiles():
    """Tra ve [(thu_muc_profile, ten_hien_thi)] doc tu Local State cua Chrome."""
    ud = chrome_userdata()
    out = []
    if not ud:
        return out
    try:
        with open(os.path.join(ud, "Local State"), "r",
                  encoding="utf-8", errors="replace") as f:
            info = json.load(f).get("profile", {}).get("info_cache", {})
        for d, meta in info.items():
            name = (meta or {}).get("name") or d
            mail = (meta or {}).get("user_name") or ""
            out.append((d, "%s%s" % (name, "  <%s>" % mail if mail else "")))
    except Exception:
        try:
            for d in sorted(os.listdir(ud)):
                if d == "Default" or d.startswith("Profile "):
                    out.append((d, d))
        except Exception:
            pass
    out.sort(key=lambda x: (x[0] != "Default", x[0]))
    return out


# ---------------------------------------------------------------------------
# Danh sach duong dan (listbox + nut)
# ---------------------------------------------------------------------------

class PathList(ttk.LabelFrame):
    def __init__(self, master, title, pick_files=False, height=6):
        super().__init__(master, text=title, padding=8)
        self.pick_files = pick_files

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        self.listbox = tk.Listbox(left, height=height, activestyle="none",
                                  selectmode="extended", font=("Consolas", 9))
        sb = ttk.Scrollbar(left, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        right = ttk.Frame(body)
        right.pack(side="left", fill="y", padx=(8, 0))
        ttk.Button(right, text="Them thu muc...", width=20,
                   command=self.add_dir).pack(pady=2)
        if pick_files:
            ttk.Button(right, text="Them file log...", width=20,
                       command=self.add_files).pack(pady=2)
        ttk.Button(right, text="Dan nhieu duong dan...", width=20,
                   command=self.paste_many).pack(pady=2)
        ttk.Button(right, text="Xoa khoi danh sach", width=20,
                   command=self.remove_sel).pack(pady=2)
        self.count = ttk.Label(right, text="Tong: 0")
        self.count.pack(pady=(8, 0))

    # -- data ---------------------------------------------------------------
    def get(self):
        return list(self.listbox.get(0, "end"))

    def set(self, paths):
        self.listbox.delete(0, "end")
        for p in paths or []:
            self.listbox.insert("end", p)
        self._refresh()

    def _add(self, paths):
        have = set(self.get())
        for p in paths:
            p = str(p).strip().strip('"')
            if p and p not in have:
                self.listbox.insert("end", p.replace("\\", "/"))
                have.add(p)
        self._refresh()

    def _refresh(self):
        self.count.configure(text="Tong: %d" % self.listbox.size())

    # -- actions ------------------------------------------------------------
    def add_dir(self):
        d = filedialog.askdirectory(title="Chon thu muc")
        if d:
            self._add([d])

    def add_files(self):
        fs = filedialog.askopenfilenames(
            title="Chon file log",
            filetypes=[("Log", "*.csv *.tsv *.json *.jsonl *.ndjson *.db *.sqlite *.sqlite3 *.txt *.log"),
                       ("Tat ca", "*.*")])
        if fs:
            self._add(fs)

    def paste_many(self):
        win = tk.Toplevel(self)
        win.title("Dan nhieu duong dan - moi dong mot cai")
        win.geometry("620x320")
        win.transient(self.winfo_toplevel())
        win.grab_set()
        ttk.Label(win, text="Dan vao day, moi dong mot duong dan:").pack(
            anchor="w", padx=10, pady=(10, 4))
        bar = ttk.Frame(win)
        bar.pack(side="bottom", fill="x", padx=10, pady=10)
        txt = tk.Text(win, font=("Consolas", 9))
        txt.pack(fill="both", expand=True, padx=10)

        def ok():
            lines = [l for l in txt.get("1.0", "end").splitlines() if l.strip()]
            self._add(lines)
            win.destroy()

        ttk.Button(bar, text="Them vao danh sach", command=ok).pack(side="right")
        ttk.Button(bar, text="Huy", command=win.destroy).pack(side="right", padx=6)
        txt.focus_set()

    def remove_sel(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
        self._refresh()


# ---------------------------------------------------------------------------
# Ghi log ra o text
# ---------------------------------------------------------------------------

class QueueWriter:
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)

    def flush(self):
        pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Vidtool - tra video theo gio dang & loc frame ro net")
        self.geometry("1000x880")
        self.minsize(900, 760)
        self.configure(bg=BG)

        self.q = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker = None
        self.last_outdir = None
        self.last_html = None
        self._flow_busy = False
        self.plus_anchor = FLOW_PLUS_ANCHOR
        self.win_size = None
        self.input_ratio = FLOW_INPUT
        self.dialog_wait = FLOW_DIALOG_WAIT
        self.menu_wait = FLOW_MENU_WAIT

        self._build()
        self._load_config()
        # may la / o doi chu cai -> hoi thu muc goc roi nap lai
        if not self._paths_alive(self._cfg_raw):
            if self.first_run_setup():
                self._load_config()
        # van chua co gi (nguoi dung bam Cancel) -> noi thang ra, dung de ho
        # bam Tra roi nhan mot cau bao loi kho hieu
        if not self._paths_alive(self._cfg_raw):
            self._say("[!] Chua tro toi thu muc nao co that tren may nay.")
            self._say("    Bam 'Chon lai thu muc' o goc duoi ben phai, "
                      "chon thu muc cha chua video va log.")
            self.status.configure(
                text="Chua co du lieu - bam 'Chon lai thu muc' de chon thu muc goc.")
        self.after(80, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- UI -----------------------------------------------------------------
    def _build(self):
        pad = dict(padx=10, pady=(8, 0))

        self.videos = PathList(self, "Thu muc luu video", height=5)
        self.videos.pack(fill="x", **pad)

        self.logs = PathList(self, "File / thu muc log cua tool quet",
                             pick_files=True, height=4)
        self.logs.pack(fill="x", **pad)

        opt = ttk.LabelFrame(self, text="Tuy chon", padding=8)
        opt.pack(fill="x", **pad)

        r1 = ttk.Frame(opt)
        r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="Gio dang video:").pack(side="left")
        self.var_time = tk.StringVar()
        e = ttk.Entry(r1, textvariable=self.var_time, width=24, font=("Consolas", 10))
        e.pack(side="left", padx=6)
        ttk.Label(r1, text="vd 2026-09-02 21:35  /  02/09/2026 21:35  /  21:35",
                  foreground="#666").pack(side="left")

        r2 = ttk.Frame(opt)
        r2.pack(fill="x", pady=6)
        self.var_tol = tk.StringVar(value="15")
        self.var_top = tk.StringVar(value="12")
        self.var_int = tk.StringVar(value="0.5")
        self.var_sharp = tk.StringVar(value="100")
        self.var_dedup = tk.StringVar(value="10")
        for label, var, w, frm, to, inc in [
            ("Sai so (phut)", self.var_tol, 6, 1, 1440, 5),
            ("So anh luu", self.var_top, 6, 1, 200, 1),
            ("Giay / frame mau", self.var_int, 6, 0.1, 10, 0.1),
            ("Do net toi thieu", self.var_sharp, 7, 0, 5000, 25),
            ("Khu trung lap", self.var_dedup, 6, 0, 32, 1),
        ]:
            ttk.Label(r2, text=label + ":").pack(side="left", padx=(0, 3))
            ttk.Spinbox(r2, textvariable=var, width=w, from_=frm, to=to,
                        increment=inc).pack(side="left", padx=(0, 14))

        r3 = ttk.Frame(opt)
        r3.pack(fill="x", pady=2)
        self.var_all = tk.BooleanVar(value=False)
        ttk.Checkbutton(r3, text="Phan tich tat ca ung vien (khong chi cai khop nhat)",
                        variable=self.var_all).pack(side="left")
        self.var_open = tk.BooleanVar(value=True)
        ttk.Checkbutton(r3, text="Xong thi mo bang anh (contact sheet)",
                        variable=self.var_open).pack(side="left", padx=20)

        r4 = ttk.Frame(opt)
        r4.pack(fill="x", pady=(8, 2))
        ttk.Label(r4, text="Chrome profile:").pack(side="left")
        self._profiles = chrome_profiles()
        self.cbo_prof = ttk.Combobox(
            r4, width=30, state="readonly",
            values=["%s  [%s]" % (name, d) for d, name in self._profiles]
                   or ["(khong tim thay Chrome)"])
        self.cbo_prof.pack(side="left", padx=6)
        ttk.Label(r4, text="Link Flow:").pack(side="left", padx=(14, 0))
        self.var_flow = tk.StringVar(value=FLOW_URL)
        ttk.Entry(r4, textvariable=self.var_flow, width=42,
                  font=("Consolas", 9)).pack(side="left", padx=6)

        r5 = ttk.Frame(opt)
        r5.pack(fill="x", pady=(4, 2))
        ttk.Label(r5, text="Them anh vao Flow:").pack(side="left")
        self.cbo_mode = ttk.Combobox(r5, width=34, state="readonly",
                                     values=[lb for lb, _m in MODE_LABELS])
        self.cbo_mode.current(0)
        self.cbo_mode.pack(side="left", padx=6)
        ttk.Label(r5, text="Doi trang load (giay):").pack(side="left", padx=(16, 3))
        self.var_delay = tk.StringVar(value=str(FLOW_DELAY))
        ttk.Spinbox(r5, textvariable=self.var_delay, width=6,
                    from_=1, to=120, increment=1).pack(side="left")
        ttk.Label(r5, text="(dung cham chuot/ban phim trong luc chay)",
                  foreground="#666").pack(side="left", padx=10)

        r6 = ttk.Frame(opt)
        r6.pack(fill="x", pady=(2, 2))
        ttk.Label(r6, text="Vi tri nut '+':").pack(side="left")
        self.lbl_plus = ttk.Label(r6, text="-", foreground="#333")
        self.lbl_plus.pack(side="left", padx=6)
        ttk.Button(r6, text="Do lai (di chuot toi nut '+')",
                   command=self.calib_plus).pack(side="left", padx=6)
        ttk.Label(r6, text="Lech menu (px):").pack(side="left", padx=(16, 3))
        self.var_ox = tk.StringVar(value=str(FLOW_UPLOAD_OFFSET[0]))
        self.var_oy = tk.StringVar(value=str(FLOW_UPLOAD_OFFSET[1]))
        ttk.Spinbox(r6, textvariable=self.var_ox, width=5,
                    from_=-400, to=400, increment=1).pack(side="left")
        ttk.Spinbox(r6, textvariable=self.var_oy, width=5,
                    from_=-400, to=400, increment=1).pack(side="left", padx=(3, 0))
        ttk.Label(r6, text="(muc 'Tai noi dung nghe nhin len' lech so voi nut '+')",
                  foreground="#666").pack(side="left", padx=8)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=10)
        self.btn_run = tk.Button(bar, text="TRA + PHAN TICH", bg=BTN_PRIMARY,
                                 font=("Segoe UI", 10, "bold"), width=20, height=2,
                                 relief="groove", command=self.do_run)
        self.btn_run.pack(side="left")
        self.btn_find = tk.Button(bar, text="CHI TRA VIDEO", width=18, height=2,
                                  relief="groove", command=self.do_find)
        self.btn_find.pack(side="left", padx=6)
        self.btn_an = tk.Button(bar, text="PHAN TICH FILE...", width=18, height=2,
                                relief="groove", command=self.do_analyze_file)
        self.btn_an.pack(side="left", padx=6)
        self.btn_stop = tk.Button(bar, text="DUNG", bg=BTN_DANGER, width=10, height=2,
                                  relief="groove", state="disabled", command=self.do_stop)
        self.btn_stop.pack(side="left", padx=6)
        self.btn_flow = tk.Button(bar, text="MO FLOW + THEM ANH", bg="#e2d6ff",
                                  width=20, height=2, relief="groove",
                                  font=("Segoe UI", 9, "bold"), command=self.do_flow)
        self.btn_flow.pack(side="left", padx=6)
        tk.Button(bar, text="Mo ket qua", width=12, height=2, relief="groove",
                  command=self.open_out).pack(side="right")
        tk.Button(bar, text="Luu cau hinh", width=12, height=2, relief="groove",
                  command=self.save_config).pack(side="right", padx=6)
        tk.Button(bar, text="Chon lai thu muc", width=15, height=2, relief="groove",
                  command=self.redo_setup).pack(side="right")

        self.status = ttk.Label(self, text="San sang.", anchor="w")
        self.status.pack(side="bottom", fill="x", padx=12, pady=6)

        resf = ttk.LabelFrame(self, text="Video tra duoc  "
                                         "(bam dup = mo video, chuot phai = mo thu muc)")
        resf.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        cols = ("lech", "file", "duong_dan")
        self.tree = ttk.Treeview(resf, columns=cols, show="headings", height=6)
        for c, txt, w in (("lech", "Lech (phut)", 90),
                          ("file", "Ten file", 280),
                          ("duong_dan", "Duong dan", 520)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w",
                             stretch=(c == "duong_dan"))
        tsb = ttk.Scrollbar(resf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", self._open_hit)
        self.tree.bind("<Button-3>", self._open_hit_folder)
        self._hits = {}

        logf = ttk.Frame(self)
        logf.pack(fill="both", expand=True, padx=10)
        self.log = tk.Text(logf, bg=LOG_BG, fg=LOG_FG, insertbackground=LOG_FG,
                           font=("Consolas", 9), wrap="none", height=14)
        sb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

    # -- setup lan dau ------------------------------------------------------
    def _paths_alive(self, cfg):
        """Config co tro toi cho nao CO THAT khong.

        Phai kiem tra ton tai that su, khong duoc chi xem danh sach co rong hay
        khong: duong dan khong co ky tu dai dien thi expand_paths tra lai y
        nguyen, nen config mac dinh "./logs" van cho ra danh sach khac rong du
        tren may chang co thu muc do - the la may la khong bao gio duoc hoi
        chon thu muc goc.
        """
        try:
            found = vidtool.expand_paths(cfg.get("log_paths") or [])
        except Exception:
            return False
        return any(os.path.exists(p) for p in found)

    def first_run_setup(self, force=False):
        """Hoi thu muc goc roi tu sinh duong dan - de app chay duoc tren may la.

        Khong luu chu cai o dia cung nhac o cho nao khac: moi duong dan deu
        sinh ra tu thu muc nguoi dung chon, nen cam o sang may khac hay o doi
        tu D: sang E: thi chi can chon lai mot lan.
        """
        if not force:
            messagebox.showinfo(
                "Cai dat lan dau",
                "Chua tim thay du lieu.\n\n"
                "Chon thu muc goc chua video va log (thu muc cha, vi du "
                "Save_Full_Video).\nApp se tu do tim cac thu muc Logs va thu muc "
                "video ben trong.")
        root = filedialog.askdirectory(title="Chon thu muc goc chua video va log")
        if not root:
            return False

        root = os.path.normpath(root).replace("\\", "/")
        logs = ["%s/**/Logs" % root]
        vids = ["%s/**/Đã Đăng" % root, "%s/**/Đăng Lỗi" % root,
                "%s/**/5_da_dang" % root]

        n_log = len(vidtool.expand_paths(logs))
        n_vid = len(vidtool.expand_paths(vids))
        if not n_log and not n_vid:
            keep = messagebox.askyesno(
                "Khong thay gi",
                "Khong thay thu muc 'Logs' hay thu muc video nao trong:\n%s\n\n"
                "Co the ban chon nham cap thu muc. Van luu chu?" % root)
            if not keep:
                return False

        cfg = dict(self._cfg_raw) if getattr(self, "_cfg_raw", None) else {}
        cfg.update(vidtool.DEFAULT_CONFIG if not cfg else {})
        cfg["log_paths"] = logs
        cfg["storage_dirs"] = vids
        cfg["output_dir"] = os.path.join(HERE, "output").replace("\\", "/")
        cfg.pop("__dt", None)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)

        messagebox.showinfo(
            "Xong",
            "Da luu cau hinh.\n\nThu muc Logs tim thay : %d\n"
            "Thu muc video tim thay: %d\n\nKet qua se nam trong: %s"
            % (n_log, n_vid, os.path.join(HERE, "output")))
        return True

    # -- danh sach video tra duoc -------------------------------------------
    def show_hits(self, cands):
        """Do ket qua tra vao bang. Goi tu luong nen -> phai day qua main loop."""
        rows = [(c["delta_sec"] / 60.0, c["path"]) for c in cands if c.get("path")]
        self.after(0, lambda: self._fill_hits(rows))

    def _fill_hits(self, rows):
        self.tree.delete(*self.tree.get_children())
        self._hits = {}
        for lech, path in rows:
            iid = self.tree.insert("", "end", values=("%.2f" % lech,
                                                      os.path.basename(path), path))
            self._hits[iid] = path

    def _selected_hit(self):
        sel = self.tree.selection() or ()
        return self._hits.get(sel[0]) if sel else None

    def _open_hit(self, _evt=None):
        p = self._selected_hit()
        if not p:
            return
        if os.path.exists(p):
            self._open(p)
        else:
            messagebox.showwarning("Khong con file", "Khong thay file:\n%s" % p)

    def _open_hit_folder(self, evt=None):
        if evt is not None:
            iid = self.tree.identify_row(evt.y)
            if iid:
                self.tree.selection_set(iid)
        p = self._selected_hit()
        if not p:
            return
        folder = os.path.dirname(p)
        if os.path.isdir(folder):
            self._open(folder)

    def redo_setup(self):
        """Chon lai thu muc goc - dung khi doi may hoac o cung doi chu cai."""
        if self.first_run_setup(force=True):
            self._load_config()
            self._say("Da chon lai thu muc goc.")
            self._say("   log  : %s" % self.logs.get())
            self._say("   video: %s" % self.videos.get())
            self.status.configure(text="San sang.")

    # -- config -------------------------------------------------------------
    def _load_config(self):
        cfg = vidtool.load_config(CONFIG_PATH)
        self.videos.set(cfg.get("storage_dirs"))
        self.logs.set(cfg.get("log_paths"))
        self.var_tol.set(str(cfg.get("tolerance_minutes", 15)))
        self.var_top.set(str(cfg.get("top_n", 12)))
        self.var_int.set(str(cfg.get("sample_interval_sec", 0.5)))
        self.var_sharp.set(str(cfg.get("min_sharpness", 100)))
        self.var_dedup.set(str(cfg.get("dedupe_hamming", 10)))
        self.out_dir = cfg.get("output_dir") or os.path.join(HERE, "output")
        self.var_flow.set(cfg.get("flow_url") or FLOW_URL)
        self.chrome_path = cfg.get("chrome_path") or chrome_exe()
        self.var_delay.set(str(cfg.get("flow_delay_sec", FLOW_DELAY)))
        want_mode = cfg.get("flow_add_mode", MODE_DIALOG)
        for i, (_lb, m) in enumerate(MODE_LABELS):
            if m == want_mode:
                self.cbo_mode.current(i)
                break
        self.dialog_wait = float(cfg.get("flow_dialog_wait_sec", FLOW_DIALOG_WAIT))
        pa = _ratio(cfg.get("flow_plus_anchor"), FLOW_PLUS_ANCHOR)
        self.plus_anchor = (int(pa[0]), int(pa[1]))
        ws = cfg.get("flow_window_size")
        self.win_size = list(ws) if ws and len(ws) == 2 else None
        self.input_ratio = _ratio(cfg.get("flow_input_ratio"), FLOW_INPUT)
        self.menu_wait = float(cfg.get("flow_menu_wait_sec", FLOW_MENU_WAIT))
        ox, oy = _ratio(cfg.get("flow_upload_offset"), FLOW_UPLOAD_OFFSET)
        self.var_ox.set(str(int(ox)))
        self.var_oy.set(str(int(oy)))
        self._show_plus()
        want = cfg.get("chrome_profile") or "Default"
        for i, (d, _n) in enumerate(self._profiles):
            if d == want:
                self.cbo_prof.current(i)
                break
        else:
            if self._profiles:
                self.cbo_prof.current(0)
        self._cfg_raw = cfg

    def profile_dir(self):
        i = self.cbo_prof.current()
        if 0 <= i < len(self._profiles):
            return self._profiles[i][0]
        return ""

    def add_mode(self):
        i = self.cbo_mode.current()
        return MODE_LABELS[i][1] if 0 <= i < len(MODE_LABELS) else MODE_DIALOG

    def upload_offset(self):
        """Do lech (px) tu nut '+' toi muc 'Tai noi dung nghe nhin len'."""
        try:
            return int(float(self.var_ox.get())), int(float(self.var_oy.get()))
        except ValueError:
            return FLOW_UPLOAD_OFFSET

    def _show_plus(self):
        self.lbl_plus.configure(text="cach mep phai %d, mep tren %d px"
                                     % (self.plus_anchor[0], self.plus_anchor[1]))

    def calib_plus(self):
        """Do lai vi tri nut '+' bang cach doc toa do chuot dang tro vao no."""
        if os.name != "nt":
            messagebox.showinfo("Chi Windows", "Chuc nang nay chi chay tren Windows.")
            return
        if getattr(self, "_calib_busy", False):
            return
        self._calib_busy = True
        self._say("Ra cua so Chrome, dat chuot dung tren nut '+' va giu yen...")
        threading.Thread(target=self._calib_worker, daemon=True).start()

    def _calib_worker(self):
        try:
            for i in (5, 4, 3, 2, 1):
                self._say("  do sau %d giay..." % i)
                time.sleep(1.0)
            hwnd, title = chrome_window_at_cursor()
            if not hwnd:
                self._say("[!] Chuot khong nam tren cua so Chrome nao. "
                          "Dua chuot dung len nut '+' cua Flow roi do lai.")
                return
            a = cursor_anchor_in_window(hwnd)
            if not a:
                self._say("[!] Chuot dang o ngoai cua so Chrome. Lam lai.")
                return
            self.plus_anchor = a
            self.win_size = list(window_size(hwnd))
            self._say("Da ghi nut '+': cach mep phai %d px, mep tren %d px "
                      "(cua so '%s' khung %dx%d)"
                      % (a[0], a[1], (title or "")[:40],
                         self.win_size[0], self.win_size[1]))
            self.after(0, self._show_plus)
            self.after(0, self.save_config)
        except Exception as e:
            self._say("[!] Loi khi do vi tri: %s" % e)
        finally:
            self._calib_busy = False

    def _cfg(self):
        cfg = dict(self._cfg_raw)
        cfg["storage_dirs"] = self.videos.get()
        cfg["log_paths"] = self.logs.get()
        cfg["output_dir"] = self.out_dir
        cfg["chrome_profile"] = self.profile_dir()
        cfg["chrome_path"] = self.chrome_path
        cfg["flow_url"] = self.var_flow.get().strip() or FLOW_URL
        cfg["flow_add_mode"] = self.add_mode()
        cfg["flow_plus_anchor"] = list(self.plus_anchor)
        if self.win_size:
            cfg["flow_window_size"] = list(self.win_size)
        cfg["flow_input_ratio"] = list(self.input_ratio)
        cfg["flow_dialog_wait_sec"] = self.dialog_wait
        cfg["flow_menu_wait_sec"] = self.menu_wait
        cfg["flow_upload_offset"] = list(self.upload_offset())
        try:
            cfg["tolerance_minutes"] = float(self.var_tol.get())
            cfg["top_n"] = int(float(self.var_top.get()))
            cfg["sample_interval_sec"] = float(self.var_int.get())
            cfg["min_sharpness"] = float(self.var_sharp.get())
            cfg["dedupe_hamming"] = int(float(self.var_dedup.get()))
            cfg["flow_delay_sec"] = max(1.0, float(self.var_delay.get()))
        except ValueError:
            raise ValueError("Co o tuy chon nhap khong phai so.")
        return cfg

    def save_config(self):
        try:
            cfg = self._cfg()
        except ValueError as e:
            messagebox.showerror("Sai gia tri", str(e))
            return
        cfg.pop("__dt", None)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        self._cfg_raw = cfg
        self._say("Da luu %s" % CONFIG_PATH)

    # -- log ----------------------------------------------------------------
    def _say(self, s):
        self.q.put(s.rstrip("\n") + "\n")

    def _drain(self):
        wrote = False
        finished = False
        flow_done = False
        try:
            while True:
                s = self.q.get_nowait()
                if s == DONE:
                    finished = True
                    continue
                if s == FLOW_DONE:
                    flow_done = True
                    continue
                self.log.insert("end", s)
                wrote = True
        except queue.Empty:
            pass
        if finished:
            self._busy(False)
            self._maybe_open()
        elif flow_done and not (self.worker and self.worker.is_alive()):
            self.status.configure(text="San sang.")
        if wrote:
            self.log.see("end")
        self.after(80, self._drain)

    def clear_log(self):
        self.log.delete("1.0", "end")

    # -- chay nen -----------------------------------------------------------
    def _busy(self, on, msg=""):
        state = "disabled" if on else "normal"
        for b in (self.btn_run, self.btn_find, self.btn_an):
            b.configure(state=state)
        self.btn_stop.configure(state="normal" if on else "disabled")
        self.status.configure(text=msg or ("Dang chay..." if on else "San sang."))

    def _start(self, fn, label, need_storage=True):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Dang ban", "Doi tac vu hien tai xong da.")
            return
        try:
            cfg = self._cfg()
        except ValueError as e:
            messagebox.showerror("Sai gia tri", str(e))
            return
        if need_storage and not cfg["storage_dirs"]:
            messagebox.showwarning("Thieu", "Chua co thu muc luu video.")
            return
        self.clear_log()
        self.stop_flag.clear()
        vidtool.CANCEL_CHECK = self.stop_flag.is_set
        self._busy(True, label)

        def run():
            w = QueueWriter(self.q)
            try:
                with contextlib.redirect_stdout(w), contextlib.redirect_stderr(w):
                    fn(cfg)
            except vidtool.Cancelled:
                self.q.put("\n[!] Da dung theo yeu cau.\n")
            except SystemExit as e:
                self.q.put("\n[!] %s\n" % e)
            except Exception:
                self.q.put("\n[LOI]\n" + traceback.format_exc())
            finally:
                self.q.put(DONE)

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def do_stop(self):
        self.stop_flag.set()
        self.status.configure(text="Dang dung...")

    def _maybe_open(self):
        if self.var_open.get() and self.last_html and os.path.exists(self.last_html):
            self._open(self.last_html)

    @staticmethod
    def _open(path):
        try:
            os.startfile(path)  # Windows
        except AttributeError:
            subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    # -- Flow ---------------------------------------------------------------
    def _shots_dir(self):
        """Thu muc anh moi nhat: uu tien lan chay vua xong, khong thi lay folder moi nhat."""
        if self.last_outdir and os.path.isdir(self.last_outdir):
            return self.last_outdir
        root = self.out_dir
        if not os.path.isdir(root):
            return None
        subs = [os.path.join(root, d) for d in os.listdir(root)]
        subs = [d for d in subs if os.path.isdir(d) and _best_jpgs(d)]
        if subs:
            return max(subs, key=os.path.getmtime)
        return root if _best_jpgs(root) else None

    def _shot_files(self):
        """(thu_muc, [duong dan anh]) - sap theo thu hang best_01, best_02..."""
        d = self._shots_dir()
        if not d:
            return None, []
        return d, [os.path.normpath(p) for p in _best_jpgs(d)]

    def do_flow(self):
        """Mo Flow dung profile Chrome roi tu them anh vua cat vao."""
        if getattr(self, "_flow_busy", False):
            messagebox.showinfo("Dang chay", "Dang them anh vao Flow, doi chut.")
            return

        url = self.var_flow.get().strip() or FLOW_URL
        prof = self.profile_dir()
        exe = self.chrome_path if os.path.isfile(self.chrome_path or "") else chrome_exe()
        folder, shots = self._shot_files()
        mode = self.add_mode() if os.name == "nt" else MODE_OFF
        try:
            delay = max(1.0, float(self.var_delay.get()))
        except ValueError:
            delay = FLOW_DELAY

        if not shots:
            self._say("[!] Chua co anh best_*.jpg trong %s - chi mo Flow thoi."
                      % (folder or self.out_dir))
            mode = MODE_OFF
        else:
            self._say("Co %d anh trong: %s" % (len(shots), folder))

        # che do dan: phai co anh trong clipboard truoc
        if mode == MODE_PASTE:
            ok, err = copy_files_to_clipboard(shots)
            if ok:
                self._say("Da copy %d anh len clipboard." % len(shots))
            else:
                self._say("[!] Khong copy duoc len clipboard: %s" % err)
                mode = MODE_OFF

        # mo Flow
        try:
            if exe:
                args = [exe]
                if prof:
                    args.append("--profile-directory=%s" % prof)
                args.append(url)
                subprocess.Popen(args)
                self._say("Mo Flow - profile: %s" % (prof or "mac dinh"))
            else:
                webbrowser.open(url)
                self._say("Khong thay chrome.exe, mo bang trinh duyet mac dinh.")
        except Exception as e:
            messagebox.showerror("Khong mo duoc Chrome", str(e))
            return

        if mode == MODE_OFF:
            if folder and os.path.isdir(folder):
                self._open(folder)
                self._say("Thu muc anh: %s - keo tha vao Flow." % folder)
            return

        self._flow_busy = True
        self.status.configure(text="Dang them anh vao Flow...")
        threading.Thread(target=self._flow_worker,
                         args=(mode, delay, folder, shots), daemon=True).start()

    def _flow_worker(self, mode, delay, folder, shots):
        try:
            self._say("Doi %.0f giay cho Flow load... "
                      "(tu day den luc xong dung cham chuot/ban phim)" % delay)
            time.sleep(delay)

            hwnd, title = find_chrome_window(size_hint=self.win_size)
            if not hwnd:
                self._say("[!] Khong thay cua so Chrome nao.")
                return
            if not focus_window(hwnd):
                self._say("[!] Windows khong cho dua Chrome len truoc. "
                          "Click vao Chrome roi lam lai.")
                return
            w, h = window_size(hwnd)
            self._say("Cua so: %s  (khung %dx%d)" % (title[:60], w, h))

            if mode == MODE_PASTE:
                self._paste_into_flow(hwnd)
            else:
                self._upload_into_flow(hwnd, shots)
        except Exception as e:
            self._say("[!] Loi khi them anh: %s" % e)
            if folder and os.path.isdir(folder):
                self._open(folder)
        finally:
            self._flow_busy = False
            self.q.put(FLOW_DONE)

    def _paste_into_flow(self, hwnd):
        x, y = click_in_window(hwnd, *self.input_ratio)
        self._say("Click o nhap tai (%d, %d) roi Ctrl+V..." % (x, y))
        send_ctrl_v()
        time.sleep(0.4)
        self._say("Da bam Ctrl+V. Flow bao 'khong co noi dung de dan' thi doi sang "
                  "che do 'Hop thoai chon file'.")

    def _upload_into_flow(self, hwnd, shots):
        """Click nut '+' cua Flow, doi hop thoai chon file hien ra roi tu dien duong dan."""
        x, y = click_at(*anchor_point(hwnd, *self.plus_anchor))
        self._say("Click nut '+' tai (%d, %d) - cach mep phai %d, mep tren %d."
                  % (x, y, self.plus_anchor[0], self.plus_anchor[1]))

        # nut "+" mo menu -> bam tiep muc "Tai noi dung nghe nhin len"
        time.sleep(self.menu_wait)
        ox, oy = self.upload_offset()
        ux, uy = x + ox, y + oy
        click_at(ux, uy)
        self._say("Click 'Tai noi dung nghe nhin len' tai (%d, %d)." % (ux, uy))

        self._say("Dang cho hop thoai chon file (toi da %.0f giay). Neu menu chua bam "
                  "trung, cu bam tay vao muc tai len - script van dang cho."
                  % self.dialog_wait)

        deadline = time.time() + self.dialog_wait
        dlg = None
        while time.time() < deadline:
            dlg = find_file_dialog()
            if dlg:
                break
            time.sleep(0.5)

        if not dlg:
            self._say("[!] Khong thay hop thoai chon file. Bam 'Do lai' de ghi dung vi tri "
                      "nut '+', hoac chinh 'Lech menu' cho khop muc tai len.")
            return

        self._say("Thay hop thoai: %s" % (_title_of(dlg) or "(khong ten)"))
        time.sleep(0.4)
        ok, err = fill_file_dialog(dlg, shots)
        if not ok:
            self._say("[!] %s" % err)
            return
        time.sleep(0.6)
        if find_file_dialog():
            self._say("[!] Hop thoai van con mo - co the Flow chi cho chon 1 anh. "
                      "Thu lai voi 1 anh, hoac tu bam Open.")
        else:
            self._say("Xong - da day %d anh vao Flow." % len(shots))

    def open_out(self):
        target = self.last_outdir or self.out_dir
        if os.path.isdir(target):
            self._open(target)
        else:
            messagebox.showinfo("Chua co", "Chua co ket qua nao trong %s" % target)

    # -- tac vu -------------------------------------------------------------
    def _find(self, cfg, verbose=True):
        t = self.var_time.get().strip()
        if not t:
            raise SystemExit("Chua nhap gio dang video.")

        # Chan tu dau khi duong dan chi dung tren giay. Khong chan thi nguoi dung
        # chi nhan duoc "Khong co ung vien nao" - doc xong van khong biet la do
        # nhap sai gio hay do chua tro dung thu muc.
        logs = [p for p in vidtool.expand_paths(cfg.get("log_paths") or [])
                if os.path.exists(p)]
        vids = [p for p in vidtool.expand_paths(cfg.get("storage_dirs") or [])
                if os.path.exists(p)]
        if not logs or not vids:
            thieu = []
            if not logs:
                thieu.append("thu muc log")
            if not vids:
                thieu.append("thu muc video")
            raise SystemExit(
                "Khong tim thay %s tren may nay.\n"
                "   Duong dan dang dung: log=%s | video=%s\n"
                "   -> Bam nut 'Chon lai thu muc' (goc duoi ben phai) roi chon "
                "thu muc cha chua video va log."
                % (" va ".join(thieu),
                   cfg.get("log_paths"), cfg.get("storage_dirs")))

        qdt, cands, _ = vidtool.find_candidates(t, cfg, verbose=verbose)
        vidtool._print_candidates(qdt, cands, 20)
        self.show_hits(cands)
        return cands

    def do_find(self):
        self._start(lambda cfg: self._find(cfg), "Dang tra video...")

    def do_run(self):
        def job(cfg):
            cands = self._find(cfg)
            found = [c for c in cands if c["path"]]
            if not found:
                print("Khong co file nao de phan tich.")
                return
            for c in (found if self.var_all.get() else found[:1]):
                print("=" * 70)
                print("Phan tich: %s" % c["path"])
                res = vidtool.analyze_video(c["path"], cfg, verbose=True)
                vidtool._print_analysis(res)
                self.last_outdir, self.last_html = res["outdir"], res["html"]
        self._start(job, "Dang tra + phan tich...")

    def do_analyze_file(self):
        files = filedialog.askopenfilenames(
            title="Chon video de phan tich",
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm *.flv *.m4v *.ts"),
                       ("Tat ca", "*.*")])
        if not files:
            return

        def job(cfg):
            for p in files:
                print("=" * 70)
                print("Phan tich: %s" % p)
                res = vidtool.analyze_video(p, cfg, verbose=True)
                vidtool._print_analysis(res)
                self.last_outdir, self.last_html = res["outdir"], res["html"]
        self._start(job, "Dang phan tich file...", need_storage=False)

    def _on_close(self):
        self.stop_flag.set()
        self.destroy()


def _report(where, exc_info):
    txt = "".join(traceback.format_exception(*exc_info))
    try:
        with open(os.path.join(HERE, "loi.log"), "a", encoding="utf-8") as f:
            f.write("\n===== %s =====\n%s" % (where, txt))
    except Exception:
        pass
    try:
        messagebox.showerror("Loi - %s" % where, txt[-1500:])
    except Exception:
        pass


def main(startup_notes=None):
    """Diem vao. launcher.py goi ham nay va dua kem ghi chu luc khoi dong."""
    try:
        app = App()
        app.report_callback_exception = lambda e, v, t: _report("giao dien", (e, v, t))
        for line in (startup_notes or []):
            app._say(line)
        app.mainloop()
    except Exception:
        _report("khoi dong", sys.exc_info())


if __name__ == "__main__":
    main()
