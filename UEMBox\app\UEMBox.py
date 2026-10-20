#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KL虚幻盒子 —— Unreal Engine 项目管理器
- 用户自定义扫描主路径与深度，遍历查找 .uproject
- 缩略图优先级: 自定义封面 > 工程目录下图片 > Saved/AutoScreenshot.png
- 显示: 名称 / 引擎版本 / 目录大小 / 上次打开日期(AutoScreenshot.png 修改时间)
- 双击打开项目；右键: 打开项目/打开目录/置顶/重命名/自定义封面
- 本程序只允许写入自己所在目录（配置、封面缓存），其余位置一律只读
"""

import hashlib
import json
import os
import shutil
import sys
import time
import winreg
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, QRectF, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap, QPen, QBrush
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QSpinBox, QStatusBar, QToolBar,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QAbstractItemView,
    QStyle, QSizePolicy, QListView, QDialog, QPushButton, QTextEdit,
)

APP_NAME = "虚幻项目管理器"        # 名称可配置：实际显示名以 config.json 的 title 为准
APP_DIR = Path(__file__).resolve().parent   # 代码所在目录: app\
BOX_ROOT = APP_DIR.parent                   # 盒子根目录: 数据文件(config/covers/cache)都在这
CONFIG_PATH = BOX_ROOT / "config.json"
COVER_DIR = BOX_ROOT / "covers"
DEFAULT_THUMB_PATH = BOX_ROOT / "DefaultThumbnail.png"   # 项目无缩略图时用的兜底图
_DEFAULT_THUMB_PM = None      # 懒加载缓存，避免每次重绘都读盘


def default_thumb_pixmap():
    """读一次默认缩略图并缓存；文件不存在/损坏则返回 None"""
    global _DEFAULT_THUMB_PM
    if _DEFAULT_THUMB_PM is None:
        pm = QPixmap(str(DEFAULT_THUMB_PATH)) if DEFAULT_THUMB_PATH.exists() else QPixmap()
        _DEFAULT_THUMB_PM = pm if not pm.isNull() else False   # False = 已确认没有
    return _DEFAULT_THUMB_PM if _DEFAULT_THUMB_PM else None

IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
PRUNE_DIRS = {".git", "node_modules", "$RECYCLE.BIN", "System Volume Information"}

DEFAULT_CONFIG = {
    "roots": [
        {"path": "U:\\UE工程", "depth": 4},
        {"path": "U:\\UE4", "depth": 4},
    ],
    "title": "虚幻项目管理器",   # 窗口/标题栏显示名，想改名字直接改这里（写什么显示什么）
    "sort_key": "时间",         # 上次使用的排序字段：时间/大小/名称/版本
    "sort_asc": False,          # 上次使用的排序方向：False=降序 True=升序
    "pinned": [],
    "alias": {},        # uproject路径 -> 显示别名
    "covers": {},       # uproject路径 -> covers/下的文件名
    "card_w": 200,
    "view_mode": "card",   # card=图标卡片 / list=横版明细
}

CARD_W = 200
CARD_H = 130          # 缩略图高度
GRID_SIZE_W = 224
GRID_SIZE_H = 214


# ---------------------------------------------------------------- 配置 ----
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    loaded = False
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
            loaded = True
    except Exception:
        loaded = False   # 文件损坏也当缺失处理，用默认值
    if not loaded:
        # config.json 不存在或损坏：自动写出一份默认配置（可随时删掉重建）
        try:
            save_config(cfg)
        except Exception:
            pass
    # 兼容旧版：把 ["X:\\a", "X:\\b"] 这种纯字符串列表升级成
    # [{"path":"X:\\a","depth":N}, ...]，并迁移旧的顶层 "depth" 默认值
    legacy_depth = int(cfg.get("depth", 4))
    raw_roots = cfg.get("roots", [])
    new_roots = []
    for r in raw_roots:
        if isinstance(r, str):
            new_roots.append({"path": r, "depth": legacy_depth})
        elif isinstance(r, dict) and r.get("path"):
            new_roots.append({"path": r["path"], "depth": int(r.get("depth", legacy_depth))})
    cfg["roots"] = new_roots
    cfg.pop("depth", None)   # 顶层 depth 已无用，避免下次写出
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("配置保存失败:", e)


# ---------------------------------------------------------------- 缓存 ----
# 项目信息记忆库：避免每次都全盘重算大小（费时、伤硬盘）
# 结构: { uproject路径: {"size": int, "size_ts": 统计时间戳} }
CACHE_PATH = BOX_ROOT / "cache.json"
SIZE_STALE_DAYS = 7          # 缓存超过这么多天视为过期
SIZE_REST_MS = 500          # 逐个补算大小之间歇多久（磁盘友好）


def load_cache() -> dict:
    try:
        if CACHE_PATH.exists():
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_cache(cache: dict) -> None:
    try:
        tmp = CACHE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)      # 原子替换，避免写一半损坏
    except Exception as e:
        print("缓存保存失败:", e)


def prune_cache(cache: dict) -> None:
    """清掉磁盘上已不存在的条目（只读 stat，不删任何文件）。"""
    for p in list(cache):
        if not Path(p).exists():
            del cache[p]


# ---------------------------------------------------------------- 工具 ----
def fmt_size(n) -> str:
    if n is None:
        return "⏳ 统计中"
    if n <= 0:
        return "0 MB"
    mb = n / 1024 / 1024
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.1f} MB"


def dir_size(path: Path) -> int:
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
            for fn in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
    except Exception:
        pass
    return total


def find_engine_exe(assoc: str):
    """按引擎版本号从注册表找 UnrealEditor.exe（只读注册表）。"""
    if not assoc or not re_ok(assoc):
        return None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(
                hive, r"SOFTWARE\EpicGames\Unreal Engine\{}".format(assoc))
            install_dir, _ = winreg.QueryValueEx(key, "InstalledDirectory")
            exe = Path(install_dir) / "Engine" / "Binaries" / "Win64" / "UnrealEditor.exe"
            if exe.exists():
                return str(exe)
        except OSError:
            continue
    return None


def re_ok(s: str) -> bool:
    """关联号可能是 '5.4' 也可能是源码构建的 GUID，这里两者都接受。"""
    return bool(s) and len(s) < 260 and "\x00" not in s


def resolve_thumb(uproj: Path, custom_cover: str | None):
    """缩略图优先级: 自定义封面 > 工程目录下的图片 > Saved/AutoScreenshot.png"""
    if custom_cover:
        p = COVER_DIR / custom_cover
        if p.exists():
            return p
    proj_dir = uproj.parent
    try:
        for entry in sorted(proj_dir.iterdir()):
            if entry.is_file() and entry.suffix.lower() in IMG_EXTS:
                return entry
    except OSError:
        pass
    auto = proj_dir / "Saved" / "AutoScreenshot.png"
    if auto.exists():
        return auto
    return None


def last_open_time(uproj: Path):
    auto = uproj.parent / "Saved" / "AutoScreenshot.png"
    try:
        if auto.exists():
            return datetime_str(auto.stat().st_mtime)
    except OSError:
        pass
    return None


def datetime_str(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# ---------------------------------------------------------------- 扫描 ----
def find_uprojects(roots):
    """每个扫描路径携带自己的遍历深度：roots 形如
    [{"path": "X:\\a", "depth": 4}, ...]
    也兼容纯字符串列表（此时统一用默认深度 4）。
    """
    found = []
    seen = set()
    for r in roots:
        if isinstance(r, dict):
            root = r.get("path")
            max_depth = int(r.get("depth", 4))
        else:
            root = r
            max_depth = 4
        root_p = Path(root)
        if not root_p.is_dir():
            continue
        base_depth = len(root_p.parts)
        for dirpath, dirnames, filenames in os.walk(root_p, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames
                           if d not in PRUNE_DIRS and not d.startswith(".")]
            cur_depth = len(Path(dirpath).parts) - base_depth
            if cur_depth >= max_depth:
                dirnames[:] = []
            for fn in filenames:
                if fn.lower().endswith(".uproject"):
                    p = str(Path(dirpath) / fn)
                    if p.lower() not in seen:
                        seen.add(p.lower())
                        found.append(Path(p))
    return found


class ScanWorker(QThread):
    """后台扫描，两阶段、硬盘友好：
    阶段1 发现：找 .uproject + 便宜信息（引擎/缩略图/时间），大小直接用缓存
    阶段2 补算：只对「新项目 / 缓存过期 / 工程有变动」的逐个算大小，项间歇气
    """
    project_ready = pyqtSignal(dict)
    size_updated = pyqtSignal(str, "qint64", float)   # 路径, 大小, 统计时间
    # ★ 大小必须用 qint64：默认 int 是 32 位 C++ int，项目超过 2GB 直接溢出成负数
    #   （曾导致大项目每次扫描都重测 + 显示 0 MB）
    scan_finished = pyqtSignal(int, int)         # 发现数, 本次实际补算数

    def __init__(self, roots, alias, covers, cache):
        super().__init__()
        self.roots = roots          # [{"path":..., "depth":...}, ...]
        self.alias = alias
        self.covers = covers
        self.cache = cache            # 引用主窗口的缓存字典，直接读写
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        uprojects = find_uprojects(self.roots)
        pending = []
        for up in uprojects:
            if self._stop:
                break
            info = self._collect_fast(up)
            if not info:
                continue
            c = self.cache.get(str(up))
            c_size = int(c.get("size", 0)) if c else 0
            if c and not self._stale(up, c) and c_size > 0:
                info["size"] = c_size
            else:
                # 没统计过 / 缓存值是 0（早年残留或中断产物）都当「未统计」，
                # 显示「统计中」并排队重测，绝不显示 0 MB
                info["size"] = None
                pending.append(up)
            self.project_ready.emit(info)

        measured = 0
        for up in pending:
            if self._stop:
                break
            size = dir_size(up.parent)
            ts = time.time()
            self.cache[str(up)] = {"size": size, "size_ts": ts}
            self.size_updated.emit(str(up), size, ts)
            measured += 1
            self.msleep(SIZE_REST_MS)   # 项目之间歇口气，别把硬盘打满
        self.scan_finished.emit(len(uprojects), measured)

    @staticmethod
    def _stale(up: Path, c: dict) -> bool:
        """缓存不可信的三种情况：太旧 / 工程文件动过 / Saved 动过(打开过)"""
        ts = c.get("size_ts", 0)
        if time.time() - ts > SIZE_STALE_DAYS * 86400:
            return True
        try:
            newest = up.stat().st_mtime
            saved = up.parent / "Saved"
            if saved.is_dir():
                newest = max(newest, saved.stat().st_mtime)
            return newest > ts
        except OSError:
            return True

    def _collect_fast(self, up: Path):
        """不碰目录大小，全是 stat/小文件读取，秒级完成"""
        try:
            engine = "N/A"
            try:
                with open(up, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                engine = str(data.get("EngineAssociation") or "N/A")
            except Exception:
                pass
            # 空项目：Content / Plugins / Source 一个都没有（只要有其中任意一个就不算空）
            is_empty = not any(
                (up.parent / d).is_dir()
                for d in ("Content", "Plugins", "Source"))
            return {
                "path": str(up),
                "dir": str(up.parent),
                "name": self.alias.get(str(up)) or up.stem,
                "engine": engine,
                "size": 0,
                "is_empty": is_empty,
                "last_open": last_open_time(up),
                "thumb": (str(t) if (t := resolve_thumb(up, self.covers.get(str(up)))) else None),
            }
        except Exception:
            return None


class SizeWorker(QThread):
    """单个项目手动重算大小（右键菜单用）"""
    done = pyqtSignal(str, "qint64", float)      # 同上：大小用 64 位防溢出

    def __init__(self, path):
        super().__init__()
        self.path = path
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        size = dir_size(Path(self.path).parent)
        self.done.emit(self.path, size, time.time())


# ------------------------------------------------------------- 卡片图标 ----
def make_card_pixmap(thumb_path: str | None, engine: str) -> QPixmap:
    """200x130 缩略图 + 右上角引擎版本角标（卡片与横版列表共用）"""
    pm = QPixmap(CARD_W, CARD_H)
    pm.fill(QColor(45, 45, 48))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

    if thumb_path and Path(thumb_path).exists():
        img = QPixmap(str(thumb_path))
        ok = not img.isNull()
    else:
        # 项目没有自带缩略图：优先用默认的 DefaultThumbnail.png（仅从这里读）
        img = default_thumb_pixmap()
        ok = img is not None
    if ok:
        scaled = img.scaled(CARD_W, CARD_H,
                            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
        x = (scaled.width() - CARD_W) // 2
        y = (scaled.height() - CARD_H) // 2
        p.drawPixmap(-x, -y, scaled)
    else:
        _draw_placeholder(p)

    # 引擎角标
    label = engine if engine else "N/A"
    f = QFont("Microsoft YaHei UI", 9)
    f.setBold(True)
    p.setFont(f)
    tw = p.fontMetrics().horizontalAdvance(label) + 14
    badge = QRectF(CARD_W - tw - 6, 6, tw, 20)
    p.setPen(QPen(QColor(0, 0, 0, 40), 1))
    p.setBrush(QColor(20, 20, 22, 200))
    p.drawRoundedRect(badge, 4, 4)
    p.setPen(QColor(255, 255, 255))
    p.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)
    p.end()
    return pm


def make_card_icon(thumb_path: str | None, engine: str) -> QIcon:
    return QIcon(make_card_pixmap(thumb_path, engine))


def _draw_placeholder(p: QPainter):
    p.fillRect(0, 0, CARD_W, CARD_H, QColor(52, 53, 58))
    f = QFont("Microsoft YaHei UI", 26)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor(90, 92, 100))
    p.drawText(0, 0, CARD_W, CARD_H - 14, Qt.AlignmentFlag.AlignCenter, "UE")
    f2 = QFont("Microsoft YaHei UI", 8)
    p.setFont(f2)
    p.drawText(0, 0, CARD_W, CARD_H, Qt.AlignmentFlag.AlignBottom |
               Qt.AlignmentFlag.AlignHCenter, "暂无预览图")


# ------------------------------------------------------------- 主窗口 ----
# ----------------------------------------------------- 可点击日志状态栏 ----
class LogWindow(QDialog):
    """右下角「运行日志」的弹窗：列出历史消息，带关闭按钮（也可点标题栏 X）"""
    def __init__(self, history, parent=None):
        super().__init__(parent)
        self.setWindowTitle("运行日志")
        self.resize(600, 380)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        lay = QVBoxLayout(self)
        te = QTextEdit()
        te.setReadOnly(True)
        te.setPlainText("\n".join(history) if history else "（暂无日志）")
        te.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        lay.addWidget(te, 1)
        btn = QPushButton("关闭")
        btn.clicked.connect(self.close)
        lay.addWidget(btn)
        self._te = te

    def refresh(self, history):
        self._te.setPlainText("\n".join(history) if history else "（暂无日志）")


class LogStatusBar(QStatusBar):
    """底部状态栏：
       - 左半区显示运行消息，单击弹「运行日志」窗口（可叉掉）
       - 右端放一个永久部件，用来显示工程总数
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._history = []
        self._log = None
        self._right = None

    def showMessage(self, message, timeout=0):
        msg = str(message)
        if not self._history or self._history[-1] != msg:
            self._history.append(msg)
        super().showMessage(msg, timeout)

    def set_right_widget(self, w):
        self._right = w
        self.addPermanentWidget(w)

    def mousePressEvent(self, e):
        # 点右端计数部件不触发；点其它区域（即左侧日志区）打开日志窗口
        if self._right and self._right.geometry().contains(e.pos()):
            super().mousePressEvent(e)
            return
        self.open_log()

    def open_log(self):
        if self._log is None:
            self._log = LogWindow(self._history, self)
            self._log.finished.connect(lambda: setattr(self, "_log", None))
        else:
            self._log.refresh(self._history)
        self._log.show()
        self._log.raise_()
        self._log.activateWindow()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.projects = {}          # path -> info
        self.worker = None
        self._size_worker = None
        self.cache = load_cache()   # 项目信息记忆库（大小等），避免重复全盘统计
        COVER_DIR.mkdir(exist_ok=True)

        # 显示名以 config.json 的 title 为准，没写就用默认值（写什么显示什么）
        self.app_name = (self.cfg.get("title") or "").strip() or APP_NAME
        self.setWindowTitle(self.app_name)
        self.resize(1180, 720)

        central = QWidget()
        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(12, 8, 12, 8)
        vbox.setSpacing(6)

        # ---- 工具条 ----
        bar = QHBoxLayout()
        bar.setSpacing(6)
        title = QLabel(f"<b style='font-size:16px'>{self.app_name}</b>")
        bar.addWidget(title)
        bar.addSpacing(12)

        bar.addWidget(QLabel("扫描路径"))
        self.root_combo = DropCombo()
        self.root_combo.setView(QListView())   # 默认弹层会把当前项顶到框沿造成遮挡，换普通列表视图
        self.root_combo.setMinimumWidth(240)
        self.root_combo.addItem("全部路径")        # 第 0 项：显示所有路径的项目
        for r in self.cfg["roots"]:
            self.root_combo.addItem(r["path"])
        self.root_combo.currentIndexChanged.connect(self.on_root_changed)
        bar.addWidget(self.root_combo)

        btn_add = QPushButton_tool("＋ 添加")
        btn_add.clicked.connect(self.add_root)
        bar.addWidget(btn_add)
        btn_del = QPushButton_tool("－ 删除")
        btn_del.clicked.connect(self.del_root)
        bar.addWidget(btn_del)

        bar.addSpacing(8)
        bar.addWidget(QLabel("深度"))
        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(1, 12)
        self.depth_spin.setToolTip("当前选中路径向下遍历的子文件夹层数（每个路径独立配置）")
        self.depth_spin.valueChanged.connect(self.on_depth_changed)
        bar.addWidget(self.depth_spin)

        btn_scan = QPushButton_tool("🔄 扫描")
        btn_scan.clicked.connect(self.start_scan)
        bar.addWidget(btn_scan)

        bar.addStretch()

        self.ver_combo = DropCombo()
        self.ver_combo.setView(QListView())
        self.ver_combo.setMinimumWidth(110)
        self.ver_combo.currentIndexChanged.connect(self.populate)
        bar.addWidget(self.ver_combo)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 搜索我的工程")
        self.search_edit.setMinimumWidth(180)
        self.search_edit.textChanged.connect(self.populate)
        bar.addWidget(self.search_edit)

        bar.addSpacing(10)
        bar.addWidget(QLabel("排序"))
        self.sort_combo = DropCombo()
        self.sort_combo.setView(QListView())
        self.sort_combo.addItems(["时间", "大小", "名称", "版本"])
        # 恢复上次使用的排序方式（先恢复方向再设字段，最后才接信号，避免启动时误写回）
        self.sort_asc = bool(self.cfg.get("sort_asc", False))
        saved_sort = self.cfg.get("sort_key", "时间")
        if self.sort_combo.findText(saved_sort) >= 0:
            self.sort_combo.setCurrentText(saved_sort)
        self.sort_combo.setToolTip("按所选字段排序（自动记住上次的选择）")
        self.sort_combo.currentIndexChanged.connect(self.on_sort_changed)
        bar.addWidget(self.sort_combo)

        self.sort_order_btn = QPushButton_tool("↑ 升序" if self.sort_asc else "↓ 降序")
        self.sort_order_btn.setToolTip("点击切换升序 / 降序（自动记住）")
        self.sort_order_btn.setMaximumWidth(72)
        self.sort_order_btn.clicked.connect(self.toggle_order)
        bar.addWidget(self.sort_order_btn)

        bar.addSpacing(10)
        bar.addWidget(QLabel("排版"))
        self.view_combo = DropCombo()
        self.view_combo.setView(QListView())
        self.view_combo.addItem("▦ 卡片", "card")
        self.view_combo.addItem("☰ 列表", "list")
        self.view_combo.setCurrentIndex(0 if self.cfg.get("view_mode", "card") == "card" else 1)
        self.view_combo.setToolTip("卡片=图标网格；列表=横版详细信息")
        self.view_combo.currentIndexChanged.connect(self.switch_view)
        bar.addWidget(self.view_combo)

        vbox.addLayout(bar)

        # ---- 卡片区 ----
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list.setIconSize(QSize(CARD_W, CARD_H))
        self.list.setGridSize(QSize(GRID_SIZE_W, GRID_SIZE_H))
        self.list.setSpacing(10)
        self.list.setWordWrap(True)
        self.list.setUniformItemSizes(True)
        self.list.itemDoubleClicked.connect(self.open_project)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.show_menu)
        vbox.addWidget(self.list)

        # ---- 横版明细视图 ----
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["名称", "版本", "上次打开", "大小", "路径"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setSortingEnabled(False)
        self.tree.setIconSize(QSize(64, 42))
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in (1, 2, 3):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.tree.itemDoubleClicked.connect(self.open_project)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_menu)
        mode0 = self.view_combo.currentData()
        self.list.setVisible(mode0 == "card")
        self.tree.setVisible(mode0 == "list")
        vbox.addWidget(self.tree)

        self.setCentralWidget(central)
        self.status = LogStatusBar()
        self.setStatusBar(self.status)
        # 右端永久显示工程总数
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color:#5a5d66; padding:0 4px;")
        self.status.set_right_widget(self.count_label)
        self.status.showMessage("就绪 —— 点击「扫描」开始查找工程（点底部左侧可看运行日志）")

        self.on_root_changed(self.root_combo.currentIndex())   # 同步深度框（默认「全部路径」禁用）
        self._rebuild_versions()
        self.start_scan()

    # ------------------------------------------------ 扫描控制 ----
    def start_scan(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1000)
        save_config(self.cfg)
        self.projects.clear()
        self._update_count()
        self.populate()
        roots = list(self.cfg["roots"])
        self.worker = ScanWorker(roots,
                                 self.cfg.get("alias", {}), self.cfg.get("covers", {}),
                                 self.cache)
        self.worker.project_ready.connect(self.on_project)
        self.worker.size_updated.connect(self.on_size_updated)
        self.worker.scan_finished.connect(self.on_scan_done)
        self.worker.start()
        self.status.showMessage(f"正在扫描 {len(roots)} 个路径（各路径深度独立配置）……"
                                "已知项目直接用缓存，只有新项目/有变动的才重算大小")

    def on_project(self, info: dict):
        """发现阶段：增量插入，不全量重建（防大量项目卡死）"""
        if info["path"] in self.projects:
            return
        self.projects[info["path"]] = info
        self._append_item(info)
        self._update_count()
        self.status.showMessage(f"正在扫描……已发现 {len(self.projects)} 个工程，"
                                "大小随后逐个统计")

    def _update_count(self):
        """右下角固定显示工程总数"""
        self.count_label.setText(f"共 {len(self.projects)} 个工程")

    def _append_item(self, info: dict):
        """把单个项目追加到两个视图末尾（发现阶段不做排序，扫描完成后统一排）"""
        if not self._passes_path(info["path"]):     # 当前只显示某条路径时，跳过其它路径
            return
        pinned = set(self.cfg.get("pinned", []))
        p = info["path"]
        card = self._make_card(info, p in pinned)
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, p)
        item.setSizeHint(card.sizeHint())
        item.setToolTip(p)
        self.list.addItem(item)
        self.list.setItemWidget(item, card)

        it = QTreeWidgetItem()
        it.setIcon(0, make_card_icon(info.get("thumb"), info["engine"]))
        it.setText(0, ("📌 " if p in pinned else "") + info["name"])
        it.setText(1, info["engine"])
        it.setText(2, info.get("last_open") or "—")
        it.setText(3, self._size_cell(info)[0])
        it.setForeground(3, self._size_cell(info)[1])
        it.setText(4, p)
        it.setData(0, Qt.ItemDataRole.UserRole, p)
        # 横版每行所有列悬停都显示完整路径
        for col in range(5):
            it.setToolTip(col, p)
        self.tree.addTopLevelItem(it)

    def on_size_updated(self, path: str, size: int, ts: float):
        """大小算完：只刷新对应的那一张卡，不全量重建"""
        info = self.projects.get(path)
        if info:
            info["size"] = size
        self.cache[path] = {"size": size, "size_ts": ts}
        self._update_single(path)
        self.status.showMessage(f"已统计：{Path(path).stem} = {fmt_size(size)}")

    def _update_single(self, path: str):
        pinned = set(self.cfg.get("pinned", []))
        info = self.projects.get(path)
        if not info:
            return
        # 卡片视图：找到对应 item，替换卡片
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == path:
                card = self._make_card(info, path in pinned)
                it.setSizeHint(card.sizeHint())
                self.list.setItemWidget(it, card)
                break
        # 横版视图：只改大小那一格（空项目保持橙色徽标）
        for i in range(self.tree.topLevelItemCount()):
            t = self.tree.topLevelItem(i)
            if t.data(0, Qt.ItemDataRole.UserRole) == path:
                txt, color = self._size_cell(info)
                t.setText(3, txt)
                t.setForeground(3, color)
                break

    def on_scan_done(self, n: int, measured: int):
        prune_cache(self.cache)
        save_cache(self.cache)
        self.populate()          # 扫描完统一排序（此时才全量重建一次）
        self._rebuild_versions()
        self._update_count()     # 右下角总数最终同步
        if measured:
            self.status.showMessage(
                f"扫描完成：共 {len(self.projects)} 个工程；"
                f"本次统计了 {measured} 个的大小，其余用缓存")
        else:
            self.status.showMessage(
                f"扫描完成：共 {len(self.projects)} 个工程；大小全部来自缓存，硬盘零压力"
                "（右键「重新统计大小」可强制刷新某个项目）")

    def recalc_size(self):
        """右键菜单：手动强制重算当前项目大小"""
        info = self._current_info()
        if not info:
            return
        if getattr(self, "_size_worker", None) and self._size_worker.isRunning():
            self.status.showMessage("已有统计在进行，稍等……")
            return
        self._size_worker = SizeWorker(info["path"])
        self._size_worker.done.connect(self.on_size_updated)
        self._size_worker.start()
        self.status.showMessage(f"正在重新统计 {info['name']} 的大小……")

    # ------------------------------------------------ 列表渲染 ----
    def _sort_value(self, info):
        f = self.sort_combo.currentText()
        if f == "大小":
            return info["size"] if info["size"] is not None else -1
        if f == "名称":
            return info["name"].lower()
        if f == "版本":
            return info["engine"]
        return info.get("last_open") or ""   # 时间

    def _path_filter_prefix(self):
        """当前选中路径的 normcase 前缀；选「全部路径」时返回 None（不过滤）。"""
        idx = self.root_combo.currentIndex()
        if idx > 0 and idx - 1 < len(self.cfg["roots"]):
            return os.path.normcase(os.path.normpath(self.cfg["roots"][idx - 1]["path"])) + os.sep
        return None

    def _passes_path(self, path: str) -> bool:
        pref = self._path_filter_prefix()
        if pref is None:
            return True
        return os.path.normcase(os.path.normpath(path)).startswith(pref)

    def _filtered(self):
        kw = self.search_edit.text().strip().lower()
        ver = self.ver_combo.currentText() if self.ver_combo.count() else "全部版本"
        pinned = set(self.cfg.get("pinned", []))

        items = [i for i in self.projects.values()
                 if (ver == "全部版本" or i["engine"] == ver)
                 and (not kw or kw in i["name"].lower() or kw in Path(i["path"]).stem.lower())
                 and self._passes_path(i["path"])]
        # 先按字段 + 升降序排
        items.sort(key=self._sort_value, reverse=not self.sort_asc)
        # 再稳定地把置顶项提到最前（不受升降序影响）
        items.sort(key=lambda i: 0 if i["path"] in pinned else 1)
        return items

    def _save_sort_pref(self):
        """把当前排序方式写进配置，下次启动照旧"""
        self.cfg["sort_key"] = self.sort_combo.currentText()
        self.cfg["sort_asc"] = self.sort_asc
        save_config(self.cfg)

    def on_sort_changed(self):
        self._save_sort_pref()
        self.populate()

    def toggle_order(self):
        self.sort_asc = not self.sort_asc
        self.sort_order_btn.setText("↑ 升序" if self.sort_asc else "↓ 降序")
        self._save_sort_pref()
        self.populate()

    def _size_cell(self, info: dict):
        """横版列表「大小」格的内容与颜色：空项目显示橙色 ⚠ 空项目"""
        if info.get("is_empty"):
            return "⚠ 空项目", QColor(217, 122, 26)     # 橙棕色，与正常大小区分
        return fmt_size(info["size"]), self.tree.palette().color(self.tree.foregroundRole())

    def populate(self):
        pinned = set(self.cfg.get("pinned", []))
        items = self._filtered()

        # ---- 卡片视图：每个项目一个带边框的框，信息分行排版 ----
        self.list.clear()
        for info in items:
            card = self._make_card(info, info["path"] in pinned)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, info["path"])
            item.setSizeHint(card.sizeHint())
            item.setToolTip(info["path"])
            self.list.addItem(item)
            self.list.setItemWidget(item, card)

        # ---- 横版明细视图 ----
        self.tree.clear()
        for info in items:
            it = QTreeWidgetItem()
            it.setIcon(0, make_card_icon(info.get("thumb"), info["engine"]))
            it.setText(0, ("📌 " if info["path"] in pinned else "") + info["name"])
            it.setText(1, info["engine"])
            it.setText(2, info.get("last_open") or "—")
            it.setText(3, self._size_cell(info)[0])
            it.setForeground(3, self._size_cell(info)[1])
            it.setText(4, info["path"])
            it.setData(0, Qt.ItemDataRole.UserRole, info["path"])
            # 横版每行所有列悬停都显示完整路径
            for col in range(5):
                it.setToolTip(col, info["path"])
            self.tree.addTopLevelItem(it)

    def _make_card(self, info: dict, pinned: bool) -> QFrame:
        """单个项目卡片：缩略图 + 名称 + 格式化信息行，外面套一个圆角边框"""
        w = QFrame()
        w.setObjectName("projCard")
        w.setToolTip(info["path"])        # 悬停整张卡片任意位置都显示完整路径
        v = QVBoxLayout(w)
        v.setContentsMargins(6, 6, 6, 8)
        v.setSpacing(3)

        # 投影，让卡片有「浮」在页面上的框感
        from PyQt6.QtWidgets import QGraphicsDropShadowEffect
        shadow = QGraphicsDropShadowEffect(w)
        shadow.setBlurRadius(16)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 45))
        w.setGraphicsEffect(shadow)

        img = QLabel()
        img.setPixmap(make_card_pixmap(info.get("thumb"), info["engine"]))
        img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(img)

        name = QLabel(("📌 " if pinned else "") + info["name"])
        f = name.font()
        f.setBold(True)
        name.setFont(f)
        v.addWidget(name)

        # 空项目徽标：橙棕色，放在大小前面（用户要求的位置）
        badge = ('<span style="color:#d97a1a">⚠ 空项目</span>&nbsp;&nbsp;'
                 if info.get("is_empty") else "")
        row1 = QLabel(f"{badge}💾 {fmt_size(info['size'])}    🛠 {info['engine']}")
        row1.setStyleSheet("color:#5a5d66; font-size:12px;")
        v.addWidget(row1)

        row2 = QLabel(f"🕒 {info.get('last_open') or '—'}")
        row2.setStyleSheet("color:#8a8d96; font-size:12px;")
        v.addWidget(row2)
        # 子控件也挂上路径，悬停图片/信息行同样显示完整路径
        for child in (img, name, row1, row2):
            child.setToolTip(info["path"])
        return w

    def switch_view(self):
        mode = self.view_combo.currentData()
        self.cfg["view_mode"] = mode
        save_config(self.cfg)
        self.list.setVisible(mode == "card")
        self.tree.setVisible(mode == "list")
        self.populate()

    def _rebuild_versions(self):
        self.ver_combo.blockSignals(True)
        self.ver_combo.clear()
        vers = sorted({i["engine"] for i in self.projects.values()},
                      key=lambda v: (v == "N/A", v))
        self.ver_combo.addItem("全部版本")
        self.ver_combo.addItems(vers)
        self.ver_combo.blockSignals(False)

    # ------------------------------------------------ 扫描路径管理 ----
    def _rebuild_root_combo(self):
        """重建路径下拉：第 0 项固定为「全部路径」，其后才是具体路径。"""
        self.root_combo.blockSignals(True)
        self.root_combo.clear()
        self.root_combo.addItem("全部路径")
        for r in self.cfg["roots"]:
            self.root_combo.addItem(r["path"])
        self.root_combo.blockSignals(False)

    def on_root_changed(self, idx: int):
        """切换显示范围：全部路径 / 某条具体路径。并同步深度框到该路径。"""
        if idx <= 0:                      # 全部路径：深度框无意义，禁用
            self.depth_spin.setEnabled(False)
        else:
            self.depth_spin.setEnabled(True)
            if idx - 1 < len(self.cfg["roots"]):
                self.depth_spin.setValue(int(self.cfg["roots"][idx - 1].get("depth", 4)))
        self.populate()

    def on_depth_changed(self, val: int):
        """改深度 = 改当前选中路径的深度（只影响该路径，不动其它路径）。"""
        idx = self.root_combo.currentIndex()
        if idx <= 0:
            return                       # 全部路径下不可改
        if idx - 1 < len(self.cfg["roots"]):
            self.cfg["roots"][idx - 1]["depth"] = val
            save_config(self.cfg)

    def add_root(self):
        d = QFileDialog.getExistingDirectory(self, "选择扫描主路径")
        if not d:
            return
        d = os.path.normpath(d)
        if any(os.path.normcase(os.path.normpath(r["path"])) == os.path.normcase(d)
               for r in self.cfg["roots"]):
            self.status.showMessage(f"{d} 已在扫描列表中")
            return
        self.cfg["roots"].append({"path": d, "depth": 4})
        save_config(self.cfg)
        self._rebuild_root_combo()
        # 选中新增路径，并把深度框同步过去
        self.root_combo.setCurrentIndex(self.root_combo.count() - 1)
        self.on_root_changed(self.root_combo.currentIndex())

    def del_root(self):
        idx = self.root_combo.currentIndex()
        if idx <= 0:
            self.status.showMessage("「全部路径」不可删除，请先在下拉里选择要删除的具体路径")
            return
        root = self.cfg["roots"][idx - 1]
        d = root["path"]
        # 停掉正在进行的扫描，避免删完又被塞回来
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(800)
        # 遗忘属于该路径下的所有项目（列表 + 缓存一起删）
        root_norm = os.path.normcase(os.path.normpath(d)) + os.sep
        gone = [p for p in list(self.projects)
                if os.path.normcase(os.path.normpath(p)).startswith(root_norm)]
        for p in gone:
            self.projects.pop(p, None)
            self.cache.pop(p, None)
        save_cache(self.cache)
        # 先切回「全部路径」，再删除，避免 combo 指针错位
        self.root_combo.setCurrentIndex(0)
        self.cfg["roots"] = [r for r in self.cfg["roots"]
                             if os.path.normcase(os.path.normpath(r["path"])) != os.path.normcase(d)]
        save_config(self.cfg)
        self._rebuild_root_combo()
        self.populate()
        self._update_count()
        self.status.showMessage(
            f"已移除扫描路径 {d}，遗忘 {len(gone)} 个项目（列表与缓存均已清除）")

    # ------------------------------------------------ 项目操作 ----
    def _current_info(self):
        """取当前选中项目：横版列表和卡片视图都支持"""
        if hasattr(self, "tree") and self.tree.isVisible():
            it = self.tree.currentItem()
            if not it:
                return None
            return self.projects.get(it.data(0, Qt.ItemDataRole.UserRole))
        item = self.list.currentItem()
        if not item:
            return None
        return self.projects.get(item.data(Qt.ItemDataRole.UserRole))

    def open_project(self, *args):
        info = self._current_info()
        if not info:
            return
        up = info["path"]
        exe = find_engine_exe(info["engine"])
        try:
            if exe:
                import subprocess
                subprocess.Popen([exe, up], cwd=str(Path(up).parent))
                self.status.showMessage(f"用 {exe} 启动 {info['name']}")
            else:
                os.startfile(up)  # 走系统关联（Epic 启动器）
                self.status.showMessage(f"通过系统关联启动 {info['name']}")
        except Exception as e:
            QMessageBox.warning(self, self.app_name, f"启动失败：{e}")

    def open_dir(self):
        info = self._current_info()
        if info and Path(info["dir"]).is_dir():
            os.startfile(info["dir"])

    def toggle_pin(self):
        info = self._current_info()
        if not info:
            return
        pinned = self.cfg.setdefault("pinned", [])
        if info["path"] in pinned:
            pinned.remove(info["path"])
        else:
            pinned.insert(0, info["path"])
        save_config(self.cfg)
        self.populate()

    def rename_project(self):
        info = self._current_info()
        if not info:
            return
        old = info["name"]
        name, ok = QInputDialog.getText(self, "重命名", "显示名称（留空恢复默认）：", text=old)
        if not ok:
            return
        alias = self.cfg.setdefault("alias", {})
        name = name.strip()
        if name and name != Path(info["path"]).stem:
            alias[info["path"]] = name
        else:
            alias.pop(info["path"], None)
        info["name"] = name or Path(info["path"]).stem
        save_config(self.cfg)
        self.populate()

    def custom_cover(self):
        info = self._current_info()
        if not info:
            return
        f, _ = QFileDialog.getOpenFileName(
            self, "选择封面图片", "", "图片 (*.png *.jpg *.jpeg *.bmp *.webp)")
        if not f:
            return
        try:
            ext = Path(f).suffix.lower() or ".png"
            key = hashlib.md5(info["path"].encode("utf-8")).hexdigest()
            dest = COVER_DIR / f"{key}{ext}"
            shutil.copyfile(f, dest)   # 只写入程序自己目录
            self.cfg.setdefault("covers", {})[info["path"]] = dest.name
            save_config(self.cfg)
            info["thumb"] = str(dest)
            self.populate()
        except Exception as e:
            QMessageBox.warning(self, self.app_name, f"封面设置失败：{e}")

    def clear_cover(self):
        info = self._current_info()
        if not info:
            return
        self.cfg.get("covers", {}).pop(info["path"], None)
        save_config(self.cfg)
        info["thumb"] = (str(t) if (t := resolve_thumb(Path(info["path"]), None)) else None)
        self.populate()

    def show_menu(self, pos):
        info = self._current_info()
        if not info:
            return
        src = self.sender()
        gp = src.mapToGlobal(pos) if isinstance(src, QWidget) else self.list.mapToGlobal(pos)
        menu = QMenu(self)
        a_open = QAction("▶ 打开项目", self)
        a_open.triggered.connect(self.open_project)
        a_dir = QAction("📁 打开目录", self)
        a_dir.triggered.connect(self.open_dir)
        pinned = info["path"] in self.cfg.get("pinned", [])
        a_pin = QAction("📌 取消置顶" if pinned else "📌 置顶项目", self)
        a_pin.triggered.connect(self.toggle_pin)
        a_ren = QAction("✏ 重命名", self)
        a_ren.triggered.connect(self.rename_project)
        a_cov = QAction("🖼 自定义封面", self)
        a_cov.triggered.connect(self.custom_cover)
        menu.addAction(a_open)
        menu.addAction(a_dir)
        menu.addSeparator()
        menu.addAction(a_pin)
        menu.addAction(a_ren)
        menu.addAction(a_cov)
        a_rs = QAction("🔄 重新统计大小", self)
        a_rs.setToolTip("强制重算该项目的目录大小（平时用缓存，不伤硬盘）")
        a_rs.triggered.connect(self.recalc_size)
        menu.addAction(a_rs)
        if info["path"] in self.cfg.get("covers", {}):
            a_cc = QAction("🚫 移除自定义封面", self)
            a_cc.triggered.connect(self.clear_cover)
            menu.addAction(a_cc)
        menu.exec(gp)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        if self._size_worker and self._size_worker.isRunning():
            self._size_worker.stop()
            self._size_worker.wait(1500)
        save_cache(self.cache)
        save_config(self.cfg)
        event.accept()


STYLE_SHEET = """
    QListWidget { background: transparent; border: none; }
    QListWidget::item { border-radius: 8px; }
    QListWidget::item:selected { background: transparent; }
    QListWidget::item:hover { background: transparent; }
    QLineEdit, QComboBox, QSpinBox { padding: 4px 8px; border: 1px solid #d8dae0;
        border-radius: 6px; background: white; color: #222; }
    QComboBox QAbstractItemView { background: white; color: #222;
        border: 1px solid #d8dae0;
        selection-background-color: rgba(30,120,255,0.18);
        selection-color: #222; outline: none; }
    QFrame#projCard { background: white; border: 1px solid #c9cdd6; border-radius: 10px; }
    QFrame#projCard:hover { border: 1px solid #4a86ff; background: #f8faff; }
    QTreeWidget { background: white; border: 1px solid #e2e4e9; border-radius: 6px; }
    QTreeWidget::item { height: 34px; }
    QTreeWidget::item:selected { background: rgba(30,120,255,0.12); color: black; }
"""


class DropCombo(QComboBox):
    """弹层固定贴在选择按钮正下方展开：
    按钮左下角 = 弹层左上角，按钮右下角 = 弹层右上角（与按钮同宽）。
    不跟随鼠标位置、也不把当前项顶到框沿。"""

    def showPopup(self):
        super().showPopup()
        popup = self.view().window()
        corner = self.mapToGlobal(self.rect().bottomLeft())   # 按钮左下角
        popup.move(corner)
        popup.resize(self.width(), popup.height())            # 与按钮同宽


def QPushButton_tool(text: str):
    from PyQt6.QtWidgets import QPushButton
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # 浅色主题微调，贴近参考图
    from PyQt6.QtGui import QPalette
    pal = app.palette()
    pal.setColor(QPalette.ColorRole.Window, QColor(244, 245, 247))
    pal.setColor(QPalette.ColorRole.Base, QColor(255, 255, 255))
    app.setPalette(pal)
    app.setStyleSheet(STYLE_SHEET)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
