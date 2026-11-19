"""貓咪存檔工作台：BCSFE 的視覺化介面

在本機 127.0.0.1 開一個小網頁，按鈕背後直接呼叫 BCSFE 的 Python 函式。
啟動：./start.sh（或 python server.py），然後打開 http://127.0.0.1:8765

Copyright (C) 2026 貓咪存檔工作台 contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

Built on BCSFE (Battle Cats Save File Editor) by fieryhenry,
https://codeberg.org/fieryhenry/BCSFE-Python — GPL-3.0-or-later.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bcsfe import core

HOST = "127.0.0.1"
PORT = int(os.environ.get("BCSFE_GUI_PORT", "8765"))
HERE = Path(__file__).resolve().parent
DEFAULT_SAVE = Path(
    os.environ.get("BCSFE_GUI_SAVE", str(Path.home() / ".local/share/bcsfe/saves/SAVE_DATA"))
).expanduser()

LOCK = threading.Lock()
STATE: dict = {"save": None, "path": None, "log": [], "dirty": False, "unlimited": False}

# 解除上限時，改用存檔欄位本身能存的最大值
I32 = 2_147_483_647
U16 = 65_535
STORAGE_LIMIT = {"leadership": 32_767}

# (欄位, 中文名稱, max_value_manager 的鍵, 需要回報伺服器的道具類型)
ITEMS = [
    ("catfood", "貓罐頭", "catfood", "CATFOOD"),
    ("xp", "XP", "xp", None),
    ("normal_tickets", "銀轉蛋券", "normal_tickets", None),
    ("rare_tickets", "稀有轉蛋券", "rare_tickets", "RARE_TICKET"),
    ("platinum_tickets", "白金轉蛋券", "platinum_tickets", "PLATINUM_TICKET"),
    ("legend_tickets", "傳說轉蛋券", "legend_tickets", "LEGEND_TICKET"),
    ("np", "NP", "np", None),
    ("leadership", "統率力", "leadership", None),
]


def log(msg: str) -> None:
    STATE["log"].append(msg)
    STATE["log"] = STATE["log"][-40:]


def mv(key: str) -> int:
    return int(getattr(core.core_data.max_value_manager, key))


def cap(key: str, field: str | None = None) -> int:
    """目前模式下允許的最大值：一般模式用遊戲上限，解除上限時用欄位上限。"""
    if STATE["unlimited"]:
        return STORAGE_LIMIT.get(field or key, I32)
    return mv(key)


def amount(body: dict | None, default: int, limit: int) -> int:
    raw = (body or {}).get("amount")
    if raw in (None, ""):
        return default
    try:
        return max(0, min(int(raw), limit))
    except (TypeError, ValueError):
        raise UserError("數量要輸入數字。")


SETTINGS_FILE = HERE / "settings.json"


def load_settings() -> None:
    try:
        STATE["unlimited"] = bool(json.loads(SETTINGS_FILE.read_text()).get("unlimited", False))
    except (OSError, ValueError):
        pass


def set_unlimited(on: bool) -> None:
    STATE["unlimited"] = bool(on)
    try:
        SETTINGS_FILE.write_text(json.dumps({"unlimited": STATE["unlimited"]}))
    except OSError:
        pass
    log("⚠ 已解除所有上限（封號風險高）" if on else "已恢復遊戲上限")


def need_save() -> core.SaveFile:
    s = STATE["save"]
    if s is None:
        raise UserError("還沒載入存檔，請先下載或載入存檔。")
    return s


class UserError(Exception):
    pass


# ---------- 讀取 / 下載 ----------

def load_local(path: str | None) -> None:
    p = Path(path).expanduser() if path else DEFAULT_SAVE
    if not p.exists():
        raise UserError(f"找不到存檔：{p}")
    data = core.Path(str(p)).read()
    cc = core.CountryCode.from_code("tw")
    try:
        s = core.SaveFile(data, cc=cc)
    except Exception:
        s = core.SaveFile(data)
    s.save_path = core.Path(str(p))
    STATE.update(save=s, path=str(p), dirty=False)
    log(f"已載入存檔：{p.name}")


def download(transfer: str, pin: str, cc_code: str) -> None:
    transfer, pin = transfer.strip(), pin.strip()
    if not transfer or not pin:
        raise UserError("請輸入引繼碼和認證碼。")
    cc = core.CountryCode.from_code(cc_code or "tw")
    gv = core.GameVersion(120200)  # BCSFE 本身也固定用這個值，下載時不影響
    handler, result = core.ServerHandler.from_codes(transfer, pin, cc, gv)
    if handler is None:
        if result is None:
            raise UserError("連不上伺服器，請檢查網路。")
        raise UserError("下載失敗：引繼碼或認證碼錯誤、已經用過，或國家代碼不對。請在手機重新產生一組碼。")
    s = handler.save_file
    DEFAULT_SAVE.parent.mkdir(parents=True, exist_ok=True)
    s.save_path = core.Path(str(DEFAULT_SAVE))
    s.to_file(s.save_path)
    STATE.update(save=s, path=str(DEFAULT_SAVE), dirty=False)
    log("已從伺服器下載存檔（原始備份在 transfer_backup）")


# ---------- 狀態 ----------

def summary() -> dict:
    s = STATE["save"]
    if s is None:
        return {"loaded": False, "log": STATE["log"]}
    items = []
    for field, name, key, _ in ITEMS:
        items.append({"field": field, "name": name, "value": int(getattr(s, field)),
                      "max": cap(key, field), "game_max": mv(key)})
    cats = s.cats.get_all_cats()
    unlocked = sum(1 for c in cats if c.unlocked)
    managed = core.BackupMetaData(s).get_managed_items()
    try:
        skills = skill_list(s)
    except Exception:
        traceback.print_exc()
        skills = []
    return {
        "unlimited": STATE["unlimited"],
        "limits": {
            "catseyes": cap("catseyes"),
            "catfruit": cap("catfruit_new" if s.game_version >= 110400 else "catfruit_old"),
            "battle_items": cap("battle_items"),
            "cat_level": U16 if STATE["unlimited"] else None,
        },
        "skills": skills,
        "item_lists": item_lists(s),
        "loaded": True,
        "path": STATE["path"],
        "version": str(s.game_version),
        "cc": str(s.cc),
        "items": items,
        "cats_total": len(cats),
        "cats_unlocked": unlocked,
        "managed_items": len(managed or []),
        "dirty": STATE["dirty"],
        "log": STATE["log"],
    }


# ---------- 修改 ----------

def set_items(values: dict) -> None:
    s = need_save()
    changed = []
    for field, name, key, managed in ITEMS:
        if field not in values:
            continue
        try:
            new = int(values[field])
        except (TypeError, ValueError):
            raise UserError(f"{name} 要輸入數字。")
        new = max(0, min(new, cap(key, field)))
        old = int(getattr(s, field))
        if new == old:
            continue
        setattr(s, field, new)
        if managed:
            core.BackupMetaData(s).add_managed_item(
                core.ManagedItem.from_change(new - old, getattr(core.ManagedItemType, managed))
            )
        changed.append(f"{name} {old:,} → {new:,}")
    if changed:
        STATE["dirty"] = True
        log("道具：" + "、".join(changed))
    else:
        log("道具沒有變動")


def all_cats(s):
    return s.cats.get_all_cats()


def act_unlock(s):
    cats = s.cats.get_cats_obtainable(s)
    if cats is None:
        raise UserError("讀不到貓咪圖鑑資料（遊戲資料可能還沒下載）。")
    for c in cats:
        c.unlock(s)
    return f"已解鎖 {len(cats)} 隻遊戲中拿得到的貓咪"


def act_upgrade(s, body=None):
    body = body or {}
    custom = STATE["unlimited"] and body.get("base") not in (None, "")
    if custom:
        try:
            base = max(1, min(int(body.get("base")), U16))
            plus = max(0, min(int(body.get("plus") or 0), U16))
        except (TypeError, ValueError):
            raise UserError("貓咪等級要輸入數字。")
    n = 0
    for c in all_cats(s):
        if not c.unlocked:
            continue
        if custom:
            c.upgrade.base = base - 1
            c.upgrade.plus = plus
            c.max_upgrade_level.base = max(c.max_upgrade_level.base, base - 1)
        else:
            ph = core.PowerUpHelper(c, s)
            ph.reset_upgrade()
            ph.upgrade_by(ph.get_max_possible_base() - 1)
            c.set_upgrade(s, core.Upgrade(ph.get_max_possible_plus(), -1), True)
        n += 1
    if custom:
        return f"⚠ 已把 {n} 隻貓咪設為 Lv.{base}+{plus}（超過遊戲上限，封號風險高）"
    return f"已把 {n} 隻已解鎖的貓咪升到最高等級和加值"


RARITY = {0: "基本", 1: "EX", 2: "稀有", 3: "激稀有", 4: "超激稀有", 5: "傳說稀有"}


def cat_list(s) -> list[dict]:
    """已擁有的貓咪：名稱、稀有度、目前等級和這隻貓自己的遊戲上限。"""
    ub = s.cats.read_unitbuy(s)
    out = []
    for c in all_cats(s):
        if not c.unlocked:
            continue
        names = c.get_names_cls(s) or []
        form = getattr(c, "current_form", 0) or 0
        name = names[form] if form < len(names) and names[form] else (names[0] if names else f"貓咪 #{c.id}")
        ph = core.PowerUpHelper(c, s)
        try:
            rarity = ub.get_cat_rarity(c.id)
        except Exception:
            rarity = -1
        out.append({
            "id": c.id, "name": name, "rarity": RARITY.get(rarity, "其他"),
            "base": c.upgrade.base + 1, "plus": c.upgrade.plus,
            "max_base": ph.get_max_possible_base(), "max_plus": ph.get_max_possible_plus(),
        })
    return out


def set_cats(values: list) -> None:
    s = need_save()
    unl = STATE["unlimited"]
    by_id = {c.id: c for c in all_cats(s)}
    changed = over = 0
    for v in values:
        c = by_id.get(int(v.get("id", -1)))
        if c is None or not c.unlocked:
            continue
        try:
            base = int(v.get("base"))
            plus = int(v.get("plus"))
        except (TypeError, ValueError):
            raise UserError("貓咪等級要輸入數字。")
        ph = core.PowerUpHelper(c, s)
        mb, mp = ph.get_max_possible_base(), ph.get_max_possible_plus()
        limit_b, limit_p = (U16, U16) if unl else (mb, mp)
        base = max(1, min(base, limit_b))
        plus = max(0, min(plus, limit_p))
        if base == c.upgrade.base + 1 and plus == c.upgrade.plus:
            continue
        if base <= mb:
            ph.reset_upgrade()
            ph.upgrade_by(base - 1)
        else:
            c.upgrade.base = base - 1
            c.max_upgrade_level.base = max(c.max_upgrade_level.base, base - 1)
        c.upgrade.plus = plus
        changed += 1
        if base > mb or plus > mp:
            over += 1
    if not changed:
        log("貓咪等級沒有變動")
        return
    STATE["dirty"] = True
    log(f"貓咪等級：改了 {changed} 隻")
    if over:
        log(f"⚠ 其中 {over} 隻超過遊戲上限（封號風險高）")


def act_true_form(s):
    s.cats.true_form_cats(s, all_cats(s), False, core.core_data.config.get_bool(core.ConfigKey.SET_CAT_CURRENT_FORMS))
    return "已把有第三型態的貓咪進化"


def act_fourth_form(s):
    s.cats.fourth_form_cats(s, all_cats(s), False, core.core_data.config.get_bool(core.ConfigKey.SET_CAT_CURRENT_FORMS))
    return "已把有第四型態的貓咪進化"


def act_talents(s):
    td = s.cats.read_talent_data(s)
    if td is None:
        raise UserError("讀不到本能資料（遊戲資料可能還沒下載）。")
    n = 0
    for c in all_cats(s):
        if not c.unlocked or c.talents is None:
            continue
        data = td.get_cat_talents(c)
        if data is None:
            continue
        _, max_levels, _, ids = data
        for i, tid in enumerate(ids):
            t = c.get_talent_from_id(tid)
            if t is not None:
                t.level = max_levels[i]
        n += 1
    return f"已把 {n} 隻貓咪的本能點滿"


def act_guide(s):
    for c in all_cats(s):
        if c.unlocked:
            c.catguide_collected = True
    return "已解鎖已擁有貓咪的圖鑑"


def act_story(s):
    for ch in s.story.get_real_chapters():
        for i in range(48):
            ch.clear_stage(i, 1, True)
    return "已通關全部主線章節"


def act_treasures(s):
    for ch in s.story.get_real_chapters():
        for st in ch.get_valid_treasure_stages():
            st.set_treasure(3)
    return "已把主線寶物全部設為最高品質（金）"


# ---------- 道具清單（道具頁） ----------

def _names_by_category(s, category, count, fallback):
    names_o = core.core_data.get_gatya_item_names(s)
    items = core.core_data.get_gatya_item_buy(s).get_by_category(category) or []
    out = []
    for i in range(count):
        n = names_o.get_name(items[i].id) if i < len(items) else None
        out.append(n or f"{fallback} {i + 1}")
    return out


def _catfruit_key(s):
    return "catfruit_new" if s.game_version >= 110400 else "catfruit_old"


def _list_groups(s):
    """每組：(鍵, 中文名稱, 取值, 寫值, 名稱, 上限的 max_value 鍵)"""
    groups = []

    def bi_get():
        return [it.amount for it in s.battle_items.items]

    def bi_set(vals):
        for it, v in zip(s.battle_items.items, vals):
            it.amount = v

    groups.append(("battle_items", "戰鬥道具", bi_get, bi_set,
                   lambda: [n or f"戰鬥道具 {i + 1}" for i, n in enumerate(s.battle_items.get_names(s) or [None] * len(s.battle_items.items))],
                   "battle_items"))
    groups.append(("catseyes", "貓眼石", lambda: list(s.catseyes), lambda v: setattr(s, "catseyes", v),
                   lambda: _names_by_category(s, 5, len(s.catseyes), "貓眼石"), "catseyes"))

    def cf_names():
        names = core.Matatabi(s).get_names() or []
        return [(names[i] if i < len(names) and names[i] else f"貓薄荷果實 {i + 1}") for i in range(len(s.catfruit))]

    groups.append(("catfruit", "貓薄荷果實 / 巨獸石", lambda: list(s.catfruit), lambda v: setattr(s, "catfruit", v),
                   cf_names, _catfruit_key(s)))
    if getattr(s, "catamins", None) is not None:
        groups.append(("catamins", "貓咪維他命", lambda: list(s.catamins), lambda v: setattr(s, "catamins", v),
                       lambda: _names_by_category(s, 6, len(s.catamins), "維他命"), "catamins"))
    if getattr(s, "treasure_chests", None) is not None:
        groups.append(("treasure_chests", "寶箱", lambda: list(s.treasure_chests), lambda v: setattr(s, "treasure_chests", v),
                       lambda: _names_by_category(s, 12, len(s.treasure_chests), "寶箱"), "treasure_chests"))
    return groups


def item_lists(s) -> list[dict]:
    out = []
    for key, title, get, _set, names, mkey in _list_groups(s):
        try:
            vals = [int(v) for v in get()]
            nm = names()
            out.append({"key": key, "title": title, "max": cap(mkey), "game_max": mv(mkey),
                        "entries": [{"name": nm[i] if i < len(nm) else f"#{i + 1}", "value": v} for i, v in enumerate(vals)]})
        except Exception:
            traceback.print_exc()
    return out


def set_item_list(key: str, values: list) -> None:
    s = need_save()
    for gkey, title, get, setter, _names, mkey in _list_groups(s):
        if gkey != key:
            continue
        cur = [int(v) for v in get()]
        limit = cap(mkey)
        new = list(cur)
        for i, v in enumerate(values[: len(cur)]):
            if v in (None, ""):
                continue
            try:
                new[i] = max(0, min(int(v), limit))
            except (TypeError, ValueError):
                raise UserError(f"{title} 要輸入數字。")
        n = sum(1 for a, b in zip(cur, new) if a != b)
        if not n:
            log(f"{title}沒有變動")
            return
        setter(new)
        STATE["dirty"] = True
        log(f"{title}：改了 {n} 項")
        return
    raise UserError(f"未知的道具類別：{key}")


def _fill_list(s, key, body):
    for gkey, title, get, _set, _n, mkey in _list_groups(s):
        if gkey == key:
            v = amount(body, cap(mkey), cap(mkey))
            set_item_list(key, [v] * len(get()))
            return f"{title}全部設為 {v:,}"
    raise UserError("這個存檔沒有這類道具。")


def act_catseyes(s, body=None):
    return _fill_list(s, "catseyes", body)


def act_catfruit(s, body=None):
    return _fill_list(s, "catfruit", body)


def act_battle_items(s, body=None):
    return _fill_list(s, "battle_items", body)


SKILL_HARD_LIMIT = U16  # 存檔欄位是 16 位元，最多 65535


def skill_list(s) -> list[dict]:
    names_o = core.core_data.get_gatya_item_names(s)
    items = core.core_data.get_gatya_item_buy(s).get_by_category(2) or []
    ad = core.core_data.get_ability_data(s)
    valid = s.special_skills.get_valid_skills()
    out = []
    for i, it in enumerate(items):
        if i >= len(valid):
            break
        a = ad.get_ability_data_item(i)
        if a is None:
            continue
        up = valid[i].upgrade
        out.append({
            "id": i,
            "name": names_o.get_name(it.id) or f"能力 {i}",
            "base": up.base + 1,
            "plus": up.plus,
            "max_base": a.max_base_level,
            "max_plus": a.max_plus_level,
        })
    return out


def set_skills(values: list, allow_over: bool) -> None:
    s = need_save()
    caps = {k["id"]: k for k in skill_list(s)}
    changed, over = [], []
    for v in values:
        sid = int(v.get("id", -1))
        if sid not in caps:
            continue
        cap = caps[sid]
        try:
            base = int(v.get("base"))
            plus = int(v.get("plus"))
        except (TypeError, ValueError):
            raise UserError(f"{cap['name']} 的等級要輸入數字。")
        base = max(1, min(base, SKILL_HARD_LIMIT))
        plus = max(0, min(plus, SKILL_HARD_LIMIT))
        if not allow_over:
            base = min(base, cap["max_base"])
            plus = min(plus, cap["max_plus"])
        if base == cap["base"] and plus == cap["plus"]:
            continue
        s.special_skills.set_upgrade(sid, core.Upgrade(plus, base - 1))
        changed.append(f"{cap['name']} {cap['base']}+{cap['plus']} → {base}+{plus}")
        if base > cap["max_base"] or plus > cap["max_plus"]:
            over.append(cap["name"])
    if not changed:
        log("特殊能力沒有變動")
        return
    STATE["dirty"] = True
    log("特殊能力：" + "、".join(changed))
    if over:
        log("⚠ 超過遊戲上限：" + "、".join(over) + "（封號風險高）")


def act_skills_max(s):
    set_skills([{"id": k["id"], "base": k["max_base"], "plus": k["max_plus"]} for k in skill_list(s)], False)
    return "特殊能力全部拉到遊戲上限"


def act_items_max(s, body=None):
    set_items({f: cap(k, f) for f, _, k, _ in ITEMS})
    return "道具數值全部拉到上限"


ACTIONS = {
    "items_max": act_items_max,
    "catseyes": act_catseyes,
    "catfruit": act_catfruit,
    "battle_items": act_battle_items,
    "unlock": act_unlock,
    "upgrade": act_upgrade,
    "true_form": act_true_form,
    "fourth_form": act_fourth_form,
    "talents": act_talents,
    "guide": act_guide,
    "skills_max": act_skills_max,
    "story": act_story,
    "treasures": act_treasures,
}


def run_action(name: str, body: dict | None = None) -> None:
    s = need_save()
    fn = ACTIONS.get(name)
    if fn is None:
        raise UserError(f"未知的操作：{name}")
    if len(inspect.signature(fn).parameters) >= 2:
        msg = fn(s, body)
    else:
        msg = fn(s)
    STATE["dirty"] = True
    log(msg)


def save_file() -> None:
    s = need_save()
    s.to_file(s.save_path)
    STATE["dirty"] = False
    log(f"已存檔到 {Path(str(s.save_path)).name}")


def upload() -> dict:
    s = need_save()
    s.to_file(s.save_path)
    result = core.ServerHandler(s).get_codes()
    s.to_file(s.save_path)
    STATE["dirty"] = False
    if result is None:
        raise UserError("上傳失敗，請稍後再試，或檢查網路。")
    t, c = result
    log("已上傳，取得新的引繼碼")
    return {"transfer": t, "confirm": c}


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 不把請求內容（含引繼碼）印到終端機
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            with LOCK:
                self._json(200, summary())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        # 只接受本機網頁送來的請求
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
            return self._json(403, {"error": "拒絕來自其他網站的請求"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}
        extra = {}
        try:
            with LOCK:
                p = self.path
                if p == "/api/load":
                    load_local(body.get("path"))
                elif p == "/api/download":
                    download(body.get("transfer", ""), body.get("pin", ""), body.get("cc", "tw"))
                elif p == "/api/items":
                    set_items(body.get("values", {}))
                elif p == "/api/skills":
                    set_skills(body.get("values", []), STATE["unlimited"])
                elif p == "/api/cats":
                    need_save()
                    extra["cats"] = cat_list(STATE["save"])
                elif p == "/api/cats_set":
                    set_cats(list(body.get("values", [])))
                    extra["cats"] = cat_list(STATE["save"])
                elif p == "/api/settings":
                    set_unlimited(bool(body.get("unlimited")))
                elif p == "/api/item_list":
                    set_item_list(str(body.get("key", "")), list(body.get("values", [])))
                elif p.startswith("/api/action/"):
                    run_action(p.rsplit("/", 1)[1], body)
                elif p == "/api/save":
                    save_file()
                elif p == "/api/upload":
                    extra["codes"] = upload()
                else:
                    return self._json(404, {"error": "not found"})
                out = summary()
            out.update(extra)
            self._json(200, out)
        except UserError as e:
            with LOCK:
                out = summary()
            out["error"] = str(e)
            self._json(400, out)
        except Exception as e:
            traceback.print_exc()
            with LOCK:
                out = summary()
            out["error"] = f"發生錯誤：{e}"
            self._json(500, out)


def main():
    core.core_data.init_data()
    load_settings()
    if DEFAULT_SAVE.exists() and "--no-autoload" not in sys.argv:
        try:
            load_local(None)
        except Exception as e:
            log(f"自動載入失敗：{e}")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"貓咪大戰爭存檔編輯器已啟動：http://{HOST}:{PORT}  （按 Ctrl+C 結束）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
