#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
updater.py - tu tai ban code moi tu GitHub.

Y tuong: file .exe chi la cai vo chua Python va OpenCV, nang va it khi doi.
Phan logic nam o may file .py nho trong thu muc "code" canh .exe. Moi lan mo
app, module nay doi chieu phien ban voi GitHub roi tai ve nhung file da doi.
Nho vay sua code xong la may nguoi khac co ban moi ngay lan mo sau, khong phai
tai lai vai tram MB.

An toan: chi tai tu dung kho cua ban khai bao ben duoi, va moi file deu phai
khop sha256 ghi trong version.json thi moi duoc ghi de.
"""

import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# SUA 3 DONG NAY CHO DUNG KHO GITHUB CUA BAN
# ---------------------------------------------------------------------------
GITHUB_USER = "hoptran2307-ops"
GITHUB_REPO = "vidtool"
GITHUB_BRANCH = "main"
# ---------------------------------------------------------------------------

VERSION_FILE = "version.json"      # ten file moc phien ban tren GitHub
INSTALLED_FILE = "installed.json"  # ban ghi phien ban dang cai, nam trong code/
TIMEOUT = 15                       # giay, cho moi lan tai


def raw_url(name):
    return ("https://raw.githubusercontent.com/%s/%s/%s/%s"
            % (GITHUB_USER, GITHUB_REPO, GITHUB_BRANCH, name))


def configured():
    """Da khai bao kho that chua - chua thi bo qua cap nhat cho im."""
    return GITHUB_USER and not GITHUB_USER.startswith("TEN_GITHUB")


def _download(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "vidtool-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def local_version(code_dir):
    return read_json(os.path.join(code_dir, INSTALLED_FILE)).get("version", "0")


def check_and_update(code_dir, log=print):
    """Doi chieu voi GitHub roi tai file da doi. Tra ve (co_cap_nhat, thong_bao).

    Khong bao gio nem loi ra ngoai: mat mang hay GitHub chet thi app van phai
    chay duoc bang ban code dang co.
    """
    if not configured():
        return False, "chua khai bao kho GitHub - bo qua cap nhat"

    try:
        remote = json.loads(_download(raw_url(VERSION_FILE)).decode("utf-8"))
    except urllib.error.URLError as e:
        return False, "khong noi duoc GitHub (%s) - chay ban dang co" % e.reason
    except Exception as e:
        return False, "khong doc duoc version.json (%s) - chay ban dang co" % e

    rver = str(remote.get("version", ""))
    lver = local_version(code_dir)
    files = remote.get("files") or {}
    if not rver or not files:
        return False, "version.json tren GitHub thieu noi dung"
    if rver == lver:
        return False, "dang dung ban moi nhat (%s)" % lver

    log("Co ban moi: %s (dang dung %s). Dang tai..." % (rver, lver))

    # tai het ve bo nho truoc, khop sha256 het roi moi ghi - tranh truong hop
    # tai do dang rot mang, ghi de nua chung lam hong app
    staged = {}
    for name, want in files.items():
        if os.path.sep in name or ".." in name:      # chan duong dan la
            return False, "ten file khong hop le trong version.json: %s" % name
        try:
            data = _download(raw_url(name))
        except Exception as e:
            return False, "tai %s that bai (%s) - giu nguyen ban cu" % (name, e)
        got = _sha256(data)
        if got != want:
            return False, ("%s sai ma kiem tra (cho %s, nhan %s) - giu ban cu"
                           % (name, want[:12], got[:12]))
        staged[name] = data

    os.makedirs(code_dir, exist_ok=True)
    for name, data in staged.items():
        dest = os.path.join(code_dir, name)
        fd, tmp = tempfile.mkstemp(dir=code_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            shutil.move(tmp, dest)
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            return False, "khong ghi duoc %s (%s)" % (name, e)

    write_json(os.path.join(code_dir, INSTALLED_FILE),
               {"version": rver, "files": files})
    return True, "da cap nhat len ban %s" % rver
