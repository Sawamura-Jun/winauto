from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk


BACKENDS = ("uia", "win32")
DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_ELEMENTS = 800
BASE_DPI = 96.0
BASE_TK_SCALING = BASE_DPI / 72.0
DEFAULT_UI_FONT_SIZE = 11
DEFAULT_CODE_FONT_SIZE = 12
EXCLUDED_TOP_LEVEL_CLASSES = {
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Progman",
    "WorkerW",
    "NotifyIconOverflowWindow",
    "DV2ControlHost",
}
EXCLUDED_TOP_LEVEL_TITLES = {
    "Program Manager",
    "タスク バー",
}
PYWINAUTO_INSTALL_HINT = (
    "pywinauto が見つかりません。\n\n"
    "このアプリを起動している Python に pywinauto をインストールしてください。\n"
    "起動中の Python:\n"
    f"{sys.executable}\n\n"
    "インストール例:\n"
    f'"{sys.executable}" -m pip install pywinauto\n\n'
    "このリポジトリの仮想環境を使う場合は、run_winauto.bat から起動してください。"
)


def enable_windows_dpi_awareness() -> None:
    """高DPI環境でTkinterの文字や部品が小さくなりすぎないようにする。"""
    if sys.platform != "win32":
        return

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def configure_tk_appearance(root: tk.Tk) -> float:
    """Windowsの表示倍率に合わせてTkのスケールと標準フォントを調整する。"""
    try:
        dpi = float(root.winfo_fpixels("1i"))
    except tk.TclError:
        dpi = BASE_DPI

    dpi = max(BASE_DPI, min(dpi, 288.0))
    scale_ratio = dpi / BASE_DPI
    root.tk.call("tk", "scaling", BASE_TK_SCALING * scale_ratio)

    # ttk部品は名前付きフォントを参照するため、ここでまとめて大きめに設定する。
    for font_name in (
        "TkDefaultFont",
        "TkTextFont",
        "TkMenuFont",
        "TkHeadingFont",
        "TkCaptionFont",
        "TkSmallCaptionFont",
        "TkIconFont",
        "TkTooltipFont",
    ):
        try:
            font = tkfont.nametofont(font_name)
            font.configure(family="Yu Gothic UI", size=DEFAULT_UI_FONT_SIZE)
        except tk.TclError:
            continue

    try:
        fixed_font = tkfont.nametofont("TkFixedFont")
        fixed_font.configure(family="Consolas", size=DEFAULT_CODE_FONT_SIZE)
    except tk.TclError:
        pass

    return scale_ratio


def scaled_pixels(value: int, scale_ratio: float) -> int:
    """固定ピクセル指定もDPIに合わせて少し広げる。"""
    return max(value, int(round(value * scale_ratio)))


@dataclass
class ElementRecord:
    """画面から取得したGUI要素の表示・出力用データ。"""

    index: int
    top_index: int
    depth: int
    is_top: bool
    title: str = ""
    control_type: str = ""
    auto_id: str = ""
    class_name: str = ""
    control_id: int | None = None
    handle: int | None = None
    process_id: int | None = None
    rectangle: tuple[int, int, int, int] | None = None
    path: str = ""
    locator: str = ""


def _safe_string(value: Any) -> str:
    """pywinautoから返る値を表示しやすい文字列に変換する。"""
    if value is None:
        return ""
    return str(value).strip()


def _safe_int(value: Any) -> int | None:
    """空値や0をNoneとして扱い、検索条件に不要な値を混ぜない。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def _read_attr(obj: Any, attr_name: str, default: Any = None) -> Any:
    """バックエンド差分を吸収しながら属性を安全に読む。"""
    try:
        return getattr(obj, attr_name)
    except Exception:
        return default


def _call_method(obj: Any, method_name: str, default: Any = None) -> Any:
    """pywinautoのラッパー呼び出しで例外が出ても一覧取得を止めない。"""
    try:
        method = getattr(obj, method_name)
        return method()
    except Exception:
        return default


def _wrapper_info(wrapper: Any) -> Any:
    return _read_attr(wrapper, "element_info")


def _wrapper_title(wrapper: Any) -> str:
    info = _wrapper_info(wrapper)
    name = _safe_string(_read_attr(info, "name"))
    if name:
        return name
    return _safe_string(_call_method(wrapper, "window_text"))


def _wrapper_control_type(wrapper: Any) -> str:
    info = _wrapper_info(wrapper)
    control_type = _safe_string(_read_attr(info, "control_type"))
    if control_type:
        return control_type
    return _safe_string(_call_method(wrapper, "friendly_class_name"))


def _wrapper_class_name(wrapper: Any) -> str:
    info = _wrapper_info(wrapper)
    class_name = _safe_string(_read_attr(info, "class_name"))
    if class_name:
        return class_name
    return _safe_string(_call_method(wrapper, "class_name"))


def _wrapper_auto_id(wrapper: Any) -> str:
    info = _wrapper_info(wrapper)
    return _safe_string(_read_attr(info, "automation_id"))


def _wrapper_control_id(wrapper: Any) -> int | None:
    info = _wrapper_info(wrapper)
    control_id = _safe_int(_read_attr(info, "control_id"))
    if control_id is not None:
        return control_id
    return _safe_int(_call_method(wrapper, "control_id"))


def _wrapper_handle(wrapper: Any) -> int | None:
    info = _wrapper_info(wrapper)
    handle = _safe_int(_read_attr(info, "handle"))
    if handle is not None:
        return handle
    return _safe_int(_read_attr(wrapper, "handle"))


def _wrapper_process_id(wrapper: Any) -> int | None:
    info = _wrapper_info(wrapper)
    process_id = _safe_int(_read_attr(info, "process_id"))
    if process_id is not None:
        return process_id
    return _safe_int(_call_method(wrapper, "process_id"))


def _wrapper_rectangle(wrapper: Any) -> tuple[int, int, int, int] | None:
    rectangle = _call_method(wrapper, "rectangle")
    if rectangle is None:
        return None

    try:
        return (
            int(rectangle.left),
            int(rectangle.top),
            int(rectangle.right),
            int(rectangle.bottom),
        )
    except Exception:
        return None


def _has_area(rectangle: tuple[int, int, int, int] | None) -> bool:
    if rectangle is None:
        return True
    left, top, right, bottom = rectangle
    return right > left and bottom > top


def _is_minimized(wrapper: Any) -> bool:
    return bool(_call_method(wrapper, "is_minimized", False))


def _is_visible(wrapper: Any) -> bool:
    visible = _call_method(wrapper, "is_visible", True)
    if visible is False:
        return False
    return _has_area(_wrapper_rectangle(wrapper))


def _element_label(record: ElementRecord) -> str:
    """階層パスに使う短い名前を作る。"""
    base = record.title or record.auto_id or record.class_name
    if base and record.control_type:
        return f"{record.control_type}: {base}"
    if base:
        return base
    if record.control_type:
        return record.control_type
    if record.handle:
        return f"handle={record.handle}"
    return "unknown"


def _make_record(
    wrapper: Any,
    *,
    index: int,
    top_index: int,
    depth: int,
    is_top: bool,
    parent_path: str = "",
) -> ElementRecord:
    record = ElementRecord(
        index=index,
        top_index=top_index,
        depth=depth,
        is_top=is_top,
        title=_wrapper_title(wrapper),
        control_type=_wrapper_control_type(wrapper),
        auto_id=_wrapper_auto_id(wrapper),
        class_name=_wrapper_class_name(wrapper),
        control_id=_wrapper_control_id(wrapper),
        handle=_wrapper_handle(wrapper),
        process_id=_wrapper_process_id(wrapper),
        rectangle=_wrapper_rectangle(wrapper),
    )
    label = _element_label(record)
    record.path = label if not parent_path else f"{parent_path} > {label}"
    return record


def is_target_top_window_record(record: ElementRecord, backend: str) -> bool:
    """タスクバー等を除外し、通常のアプリウィンドウだけを対象にする。"""
    if record.class_name in EXCLUDED_TOP_LEVEL_CLASSES:
        return False
    if record.title in EXCLUDED_TOP_LEVEL_TITLES:
        return False

    if backend == "uia":
        return record.control_type == "Window"

    return bool(record.title or record.class_name)


def _is_target_top_window(wrapper: Any, backend: str) -> bool:
    """画面上に開いているトップレベルウィンドウかを判定する。"""
    if not _is_visible(wrapper) or _is_minimized(wrapper):
        return False

    record = _make_record(
        wrapper,
        index=0,
        top_index=0,
        depth=0,
        is_top=True,
    )
    return is_target_top_window_record(record, backend)


def _top_criteria(record: ElementRecord, backend: str) -> dict[str, Any]:
    """トップレベルウィンドウ用のpywinauto検索条件を作る。"""
    criteria: dict[str, Any] = {}
    if record.title:
        criteria["title"] = record.title

    if backend == "uia":
        if record.control_type:
            criteria["control_type"] = record.control_type
        else:
            criteria["control_type"] = "Window"
        if record.class_name:
            criteria["class_name"] = record.class_name
    else:
        if record.class_name:
            criteria["class_name"] = record.class_name

    if not criteria and record.handle:
        criteria["handle"] = record.handle
    return criteria


def _child_criteria(record: ElementRecord, backend: str) -> dict[str, Any]:
    """子要素用のpywinauto検索条件を作る。"""
    criteria: dict[str, Any] = {}
    if record.title:
        criteria["title"] = record.title

    if backend == "uia":
        if record.auto_id:
            criteria["auto_id"] = record.auto_id
        if record.control_type:
            criteria["control_type"] = record.control_type
        if record.class_name:
            criteria["class_name"] = record.class_name
    else:
        if record.class_name:
            criteria["class_name"] = record.class_name
        if record.control_id is not None:
            criteria["control_id"] = record.control_id

    if not criteria and record.handle:
        criteria["handle"] = record.handle
    return criteria


def _criteria_key(criteria: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(criteria.items())


def build_locator_expression(
    base_expression: str,
    method_name: str,
    criteria: dict[str, Any],
    *,
    found_index: int | None = None,
) -> str:
    """コピペしやすいpywinautoの呼び出し式を組み立てる。"""
    args = [f"{name}={value!r}" for name, value in criteria.items()]
    if found_index is not None:
        args.append(f"found_index={found_index}")
    return f"{base_expression}.{method_name}({', '.join(args)})"


def attach_locators(records: list[ElementRecord], backend: str) -> list[ElementRecord]:
    """重複要素にはfound_indexを付けてlocator文字列を付与する。"""
    top_criteria_by_index: dict[int, dict[str, Any]] = {}
    top_key_counts: Counter[tuple[tuple[str, Any], ...]] = Counter()

    for record in records:
        if record.is_top:
            criteria = _top_criteria(record, backend)
            top_criteria_by_index[record.top_index] = criteria
            top_key_counts[_criteria_key(criteria)] += 1

    top_seen: defaultdict[tuple[tuple[str, Any], ...], int] = defaultdict(int)
    top_expression_by_index: dict[int, str] = {}
    for record in records:
        if not record.is_top:
            continue
        criteria = top_criteria_by_index[record.top_index]
        key = _criteria_key(criteria)
        found_index = top_seen[key] if top_key_counts[key] > 1 else None
        top_seen[key] += 1
        expression = build_locator_expression(
            "desktop",
            "window",
            criteria,
            found_index=found_index,
        )
        record.locator = expression
        top_expression_by_index[record.top_index] = expression

    child_key_counts: Counter[tuple[int, tuple[tuple[str, Any], ...]]] = Counter()
    child_criteria_by_index: dict[int, dict[str, Any]] = {}
    for record in records:
        if record.is_top:
            continue
        criteria = _child_criteria(record, backend)
        child_criteria_by_index[record.index] = criteria
        child_key_counts[(record.top_index, _criteria_key(criteria))] += 1

    child_seen: defaultdict[tuple[int, tuple[tuple[str, Any], ...]], int] = defaultdict(int)
    for record in records:
        if record.is_top:
            continue
        criteria = child_criteria_by_index[record.index]
        key = (record.top_index, _criteria_key(criteria))
        found_index = child_seen[key] if child_key_counts[key] > 1 else None
        child_seen[key] += 1
        top_expression = top_expression_by_index.get(record.top_index, "desktop")
        record.locator = build_locator_expression(
            top_expression,
            "child_window",
            criteria,
            found_index=found_index,
        )

    return records


def _format_rectangle(rectangle: tuple[int, int, int, int] | None) -> str:
    if rectangle is None:
        return ""
    left, top, right, bottom = rectangle
    return f"rect=({left}, {top}, {right}, {bottom})"


def _format_record_comment(record: ElementRecord) -> str:
    parts = [
        f"{record.index:04d}",
        f"depth={record.depth}",
    ]
    if record.control_type:
        parts.append(f"type={record.control_type!r}")
    if record.title:
        parts.append(f"title={record.title!r}")
    if record.auto_id:
        parts.append(f"auto_id={record.auto_id!r}")
    if record.class_name:
        parts.append(f"class={record.class_name!r}")
    if record.control_id is not None:
        parts.append(f"control_id={record.control_id}")
    if record.process_id:
        parts.append(f"pid={record.process_id}")
    rectangle = _format_rectangle(record.rectangle)
    if rectangle:
        parts.append(rectangle)
    return "# " + " | ".join(parts)


def format_element_list(
    records: list[ElementRecord],
    backend: str,
    *,
    generated_at: datetime | None = None,
) -> str:
    """画面表示・クリップボード貼り付け用のテキストを作る。"""
    generated_at = generated_at or datetime.now()
    attach_locators(records, backend)

    lines = [
        "# pywinauto GUI要素一覧",
        f"# 取得日時: {generated_at:%Y-%m-%d %H:%M:%S}",
        f"# 要素数: {len(records)}",
        "from pywinauto import Desktop",
        f"desktop = Desktop(backend={backend!r})",
        "",
    ]

    for record in records:
        lines.append(_format_record_comment(record))
        if record.path:
            lines.append(f"# path: {record.path}")
        lines.append(record.locator)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def enumerate_gui_elements(
    *,
    backend: str = "uia",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_elements: int = DEFAULT_MAX_ELEMENTS,
    visible_only: bool = True,
    open_windows_only: bool = True,
    exclude_process_id: int | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> list[ElementRecord]:
    """表示中のGUI要素をpywinautoで列挙する。"""
    if backend not in BACKENDS:
        raise ValueError(f"未対応のbackendです: {backend}")

    try:
        from pywinauto import Desktop
    except ModuleNotFoundError as exc:
        if exc.name == "pywinauto":
            raise RuntimeError(PYWINAUTO_INSTALL_HINT) from exc
        raise

    def notify(message: str) -> None:
        if status_callback is not None:
            status_callback(message)

    desktop = Desktop(backend=backend)
    if open_windows_only:
        notify("開いているウィンドウを取得しています...")
    else:
        notify("トップレベル要素を取得しています...")
    windows = desktop.windows(visible_only=visible_only)

    records: list[ElementRecord] = []
    next_index = 1

    def add_record(
        wrapper: Any,
        *,
        top_index: int,
        depth: int,
        is_top: bool,
        parent_path: str = "",
    ) -> ElementRecord | None:
        nonlocal next_index
        if len(records) >= max_elements:
            return None
        if exclude_process_id is not None and _wrapper_process_id(wrapper) == exclude_process_id:
            return None
        if visible_only and not _is_visible(wrapper):
            return None
        if is_top and open_windows_only and not _is_target_top_window(wrapper, backend):
            return None

        record = _make_record(
            wrapper,
            index=next_index,
            top_index=top_index,
            depth=depth,
            is_top=is_top,
            parent_path=parent_path,
        )
        next_index += 1
        records.append(record)
        return record

    def walk_children(wrapper: Any, *, top_index: int, depth: int, parent_path: str) -> None:
        if len(records) >= max_elements or depth >= max_depth:
            return
        children = _call_method(wrapper, "children", [])
        for child in children:
            if len(records) >= max_elements:
                return
            child_record = add_record(
                child,
                top_index=top_index,
                depth=depth + 1,
                is_top=False,
                parent_path=parent_path,
            )
            if child_record is not None:
                walk_children(
                    child,
                    top_index=top_index,
                    depth=depth + 1,
                    parent_path=child_record.path,
                )

    top_index = 0
    for window in windows:
        if len(records) >= max_elements:
            break
        top_index += 1
        top_record = add_record(window, top_index=top_index, depth=0, is_top=True)
        if top_record is None:
            continue
        notify(f"{top_record.title or top_record.class_name or top_record.handle} を走査しています...")
        walk_children(window, top_index=top_index, depth=0, parent_path=top_record.path)

    attach_locators(records, backend)
    return records


class GuiInspectorApp(tk.Tk):
    """現在画面のGUI要素を一覧表示するデスクトップアプリ。"""

    def __init__(self) -> None:
        super().__init__()
        self.ui_scale = configure_tk_appearance(self)
        self.title("pywinauto GUI要素一覧")
        self.geometry(
            f"{scaled_pixels(1120, self.ui_scale)}x{scaled_pixels(760, self.ui_scale)}"
        )
        self.minsize(
            scaled_pixels(860, self.ui_scale),
            scaled_pixels(520, self.ui_scale),
        )

        self.backend_var = tk.StringVar(value="uia")
        self.max_depth_var = tk.IntVar(value=DEFAULT_MAX_DEPTH)
        self.max_elements_var = tk.IntVar(value=DEFAULT_MAX_ELEMENTS)
        self.open_windows_only_var = tk.BooleanVar(value=True)
        self.exclude_self_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="準備完了")

        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._worker: threading.Thread | None = None

        self._build_widgets()
        self.after(100, self._poll_queue)
        self.after(300, self.refresh)

    def _build_widgets(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        controls = ttk.Frame(root)
        controls.pack(fill=tk.X)

        options = ttk.Frame(root)
        options.pack(fill=tk.X, pady=(8, 0))

        ttk.Label(controls, text="backend").pack(side=tk.LEFT)
        backend_combo = ttk.Combobox(
            controls,
            textvariable=self.backend_var,
            values=BACKENDS,
            state="readonly",
            width=8,
        )
        backend_combo.pack(side=tk.LEFT, padx=(6, 14))

        ttk.Label(controls, text="最大階層").pack(side=tk.LEFT)
        ttk.Spinbox(
            controls,
            from_=1,
            to=10,
            textvariable=self.max_depth_var,
            width=5,
        ).pack(side=tk.LEFT, padx=(6, 14))

        ttk.Label(controls, text="最大要素数").pack(side=tk.LEFT)
        ttk.Spinbox(
            controls,
            from_=50,
            to=5000,
            increment=50,
            textvariable=self.max_elements_var,
            width=7,
        ).pack(side=tk.LEFT, padx=(6, 14))

        self.refresh_button = ttk.Button(controls, text="再取得", command=self.refresh)
        self.refresh_button.pack(side=tk.LEFT)

        ttk.Button(controls, text="全てコピー", command=self.copy_all).pack(
            side=tk.LEFT,
            padx=(8, 0),
        )

        ttk.Checkbutton(
            options,
            text="開いているウィンドウのみ",
            variable=self.open_windows_only_var,
        ).pack(side=tk.LEFT, padx=(0, 18))

        ttk.Checkbutton(
            options,
            text="このアプリを除外",
            variable=self.exclude_self_var,
        ).pack(side=tk.LEFT)

        text_frame = ttk.Frame(root)
        text_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 8))

        self.text = tk.Text(
            text_frame,
            wrap=tk.NONE,
            undo=False,
            font=tkfont.nametofont("TkFixedFont"),
        )
        y_scroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=self.text.yview)
        x_scroll = ttk.Scrollbar(text_frame, orient=tk.HORIZONTAL, command=self.text.xview)
        self.text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        self.text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)

        status = ttk.Label(root, textvariable=self.status_var, anchor=tk.W)
        status.pack(fill=tk.X)

        self.text.bind("<Control-a>", self._select_all)

    def _select_all(self, _event: tk.Event) -> str:
        self.text.tag_add(tk.SEL, "1.0", tk.END)
        self.text.mark_set(tk.INSERT, "1.0")
        self.text.see(tk.INSERT)
        return "break"

    def _set_busy(self, busy: bool) -> None:
        self.refresh_button.configure(state=tk.DISABLED if busy else tk.NORMAL)

    def _poll_queue(self) -> None:
        while True:
            try:
                event, payload = self._queue.get_nowait()
            except queue.Empty:
                break

            if event == "status":
                self.status_var.set(str(payload))
            elif event == "done":
                text, count = payload
                self.text.delete("1.0", tk.END)
                self.text.insert("1.0", text)
                self.status_var.set(f"{count}件のGUI要素を取得しました")
                self._set_busy(False)
            elif event == "error":
                self._set_busy(False)
                self.status_var.set("取得に失敗しました")
                self.text.delete("1.0", tk.END)
                self.text.insert("1.0", str(payload))
                messagebox.showerror("取得エラー", "GUI要素の取得に失敗しました。詳細は画面のログを確認してください。")

        self.after(100, self._poll_queue)

    def _validated_int(self, variable: tk.IntVar, default: int, *, minimum: int, maximum: int) -> int:
        try:
            value = int(variable.get())
        except (TypeError, ValueError, tk.TclError):
            value = default
        return max(minimum, min(maximum, value))

    def refresh(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            self.status_var.set("取得処理が実行中です")
            return

        backend = self.backend_var.get()
        max_depth = self._validated_int(
            self.max_depth_var,
            DEFAULT_MAX_DEPTH,
            minimum=1,
            maximum=10,
        )
        max_elements = self._validated_int(
            self.max_elements_var,
            DEFAULT_MAX_ELEMENTS,
            minimum=50,
            maximum=5000,
        )
        open_windows_only = self.open_windows_only_var.get()
        exclude_process_id = os.getpid() if self.exclude_self_var.get() else None

        self._set_busy(True)
        self.status_var.set("GUI要素を取得しています...")

        def worker() -> None:
            try:
                records = enumerate_gui_elements(
                    backend=backend,
                    max_depth=max_depth,
                    max_elements=max_elements,
                    open_windows_only=open_windows_only,
                    exclude_process_id=exclude_process_id,
                    status_callback=lambda message: self._queue.put(("status", message)),
                )
                text = format_element_list(records, backend)
                self._queue.put(("done", (text, len(records))))
            except Exception:
                self._queue.put(("error", traceback.format_exc()))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def copy_all(self) -> None:
        content = self.text.get("1.0", "end-1c")
        if not content:
            self.status_var.set("コピーする内容がありません")
            return
        self.clipboard_clear()
        self.clipboard_append(content)
        self.status_var.set("一覧をクリップボードにコピーしました")


def main() -> None:
    enable_windows_dpi_awareness()
    app = GuiInspectorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
