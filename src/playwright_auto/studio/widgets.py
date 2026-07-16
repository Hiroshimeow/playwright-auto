from __future__ import annotations

import tkinter as tk
from datetime import datetime, timezone
from tkinter import scrolledtext, ttk
from typing import Callable, Sequence

from .models import WorkerModel, WorkerStatus
from .theme import COLORS, GLOW_COLORS, STATUS_COLORS

RoleCallback = Callable[[str, str], None]
BoolCallback = Callable[[str, bool], None]
PageCallback = Callable[[str], None]
ReorderCallback = Callable[[str, int], None]


def drop_target_index(bounds: Sequence[tuple[int, int]], y: int) -> int:
    if not bounds:
        return 0
    for index, (top, bottom) in enumerate(bounds):
        if y <= (top + bottom) / 2:
            return index
    return len(bounds) - 1


def _elapsed_text(started_at: str | None, finished_at: str | None = None) -> str:
    if not started_at:
        return ""
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(finished_at) if finished_at else datetime.now(timezone.utc)
        seconds = max(int((end - start).total_seconds()), 0)
    except ValueError:
        return ""
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


class WorkerCard(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        *,
        model: WorkerModel,
        selected: bool = False,
        role_values: Sequence[str] = ("PLAN", "DEV", "REVIEW", "TEST"),
        on_select: PageCallback | None = None,
        on_role_change: RoleCallback | None = None,
        on_enabled: BoolCallback | None = None,
        on_edit_prompt: PageCallback | None = None,
        on_view_response: PageCallback | None = None,
        on_stop: PageCallback | None = None,
        on_release: PageCallback | None = None,
        on_drag_start: Callable[[str, int], None] | None = None,
        on_drag_end: Callable[[str, int], None] | None = None,
    ) -> None:
        super().__init__(
            master,
            bg=COLORS["card"],
            highlightthickness=2,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["border"],
            bd=0,
            padx=0,
            pady=0,
        )
        self.model = model
        self.on_select = on_select
        self.on_role_change = on_role_change
        self.on_enabled = on_enabled
        self.on_edit_prompt = on_edit_prompt
        self.on_view_response = on_view_response
        self.on_stop = on_stop
        self.on_release = on_release
        self.on_drag_start = on_drag_start
        self.on_drag_end = on_drag_end
        self.role_values = tuple(dict.fromkeys((*role_values, model.role or "")))
        self.status_text = tk.StringVar()
        self.role_var = tk.StringVar(value=model.role or "")
        self.enabled_var = tk.BooleanVar(value=model.enabled)
        self.order_text = tk.StringVar()
        self.title_text = tk.StringVar()
        self.page_text = tk.StringVar()
        self.prompt_text = tk.StringVar()
        self.elapsed_text = tk.StringVar()
        self.selected = selected
        self.glow_active = False
        self._glow_index = 0
        self._glow_after: str | None = None
        self._tick_after: str | None = None
        self._build()
        self.update_model(model, selected=selected)
        self._tick()

    def _build(self) -> None:
        body = tk.Frame(self, bg=COLORS["card"], padx=12, pady=10)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(2, weight=1)

        self.drag_handle = tk.Label(
            body,
            text="⋮⋮",
            bg=COLORS["card"],
            fg=COLORS["muted"],
            font=("Segoe UI", 14, "bold"),
            cursor="fleur",
            padx=3,
        )
        self.drag_handle.grid(row=0, column=0, rowspan=3, sticky="nsw", padx=(0, 8))
        self.drag_handle.bind("<ButtonPress-1>", self._drag_start)
        self.drag_handle.bind("<ButtonRelease-1>", self._drag_end)

        tk.Label(
            body,
            textvariable=self.order_text,
            bg=COLORS["panel_alt"],
            fg=COLORS["text"],
            font=("Segoe UI Semibold", 9),
            padx=7,
            pady=3,
        ).grid(row=0, column=1, sticky="w")

        self.role_combo = ttk.Combobox(
            body,
            textvariable=self.role_var,
            values=self.role_values,
            style="Studio.TCombobox",
            width=16,
        )
        self.role_combo.grid(row=0, column=2, sticky="ew", padx=(8, 8))
        self.role_combo.bind("<<ComboboxSelected>>", self._role_changed)
        self.role_combo.bind("<Return>", self._role_changed)
        self.role_combo.bind("<FocusOut>", self._role_changed)

        self.status_label = tk.Label(
            body,
            textvariable=self.status_text,
            bg=COLORS["panel_alt"],
            fg=COLORS["muted"],
            font=("Segoe UI Semibold", 8),
            padx=8,
            pady=4,
        )
        self.status_label.grid(row=0, column=3, sticky="e")

        self.enable_check = ttk.Checkbutton(
            body,
            text="Use",
            variable=self.enabled_var,
            style="Studio.TCheckbutton",
            command=self._enabled_changed,
        )
        self.enable_check.grid(row=0, column=4, sticky="e", padx=(8, 0))

        tk.Label(
            body,
            textvariable=self.title_text,
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI Semibold", 10),
            anchor="w",
        ).grid(row=1, column=1, columnspan=3, sticky="ew", pady=(8, 0))
        tk.Label(
            body,
            textvariable=self.elapsed_text,
            bg=COLORS["card"],
            fg=COLORS["accent"],
            font=("Consolas", 9),
            anchor="e",
        ).grid(row=1, column=4, sticky="e", pady=(8, 0))

        tk.Label(
            body,
            textvariable=self.page_text,
            bg=COLORS["card"],
            fg=COLORS["muted"],
            font=("Consolas", 8),
            anchor="w",
        ).grid(row=2, column=1, columnspan=4, sticky="ew", pady=(2, 0))

        preview = tk.Label(
            body,
            textvariable=self.prompt_text,
            bg=COLORS["input"],
            fg=COLORS["muted"],
            anchor="nw",
            justify="left",
            wraplength=560,
            padx=9,
            pady=7,
        )
        preview.grid(row=3, column=1, columnspan=4, sticky="ew", pady=(9, 8))

        actions = tk.Frame(body, bg=COLORS["card"])
        actions.grid(row=4, column=1, columnspan=4, sticky="ew")
        for column in range(4):
            actions.grid_columnconfigure(column, weight=1)
        ttk.Button(
            actions,
            text="Edit Prompt",
            style="Quiet.TButton",
            command=lambda: self._call(self.on_edit_prompt),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(
            actions,
            text="View Response",
            style="Quiet.TButton",
            command=lambda: self._call(self.on_view_response),
        ).grid(row=0, column=1, sticky="ew", padx=4)
        self.stop_button = ttk.Button(
            actions,
            text="Stop",
            style="Danger.TButton",
            command=lambda: self._call(self.on_stop),
        )
        self.stop_button.grid(row=0, column=2, sticky="ew", padx=4)
        ttk.Button(
            actions,
            text="Release",
            style="Quiet.TButton",
            command=lambda: self._call(self.on_release),
        ).grid(row=0, column=3, sticky="ew", padx=(4, 0))

        for widget in (body, preview):
            widget.bind("<Button-1>", lambda _event: self._call(self.on_select))

    def _call(self, callback: PageCallback | None) -> None:
        if callback is not None:
            callback(self.model.page_id)

    def _role_changed(self, _event: tk.Event | None = None) -> None:
        role = self.role_var.get().strip()
        if role and role != (self.model.role or "") and self.on_role_change is not None:
            self.on_role_change(self.model.page_id, role)

    def _enabled_changed(self) -> None:
        if self.on_enabled is not None:
            self.on_enabled(self.model.page_id, bool(self.enabled_var.get()))

    def _drag_start(self, event: tk.Event) -> None:
        if self.on_drag_start is not None:
            self.on_drag_start(self.model.page_id, event.y_root)

    def _drag_end(self, event: tk.Event) -> None:
        if self.on_drag_end is not None:
            self.on_drag_end(self.model.page_id, event.y_root)

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        self.elapsed_text.set(_elapsed_text(self.model.started_at, self.model.finished_at))
        self._tick_after = self.after(1000, self._tick)

    def _set_glow(self, active: bool) -> None:
        self.glow_active = active
        if not active:
            if self._glow_after is not None:
                self.after_cancel(self._glow_after)
                self._glow_after = None
            border = COLORS["blue"] if self.selected else COLORS["border"]
            self.configure(highlightbackground=border, highlightcolor=border)
            return
        if self._glow_after is None:
            self._animate_glow()

    def _animate_glow(self) -> None:
        if not self.glow_active or not self.winfo_exists():
            self._glow_after = None
            return
        color = GLOW_COLORS[self._glow_index % len(GLOW_COLORS)]
        self._glow_index += 1
        self.configure(highlightbackground=color, highlightcolor=color)
        self._glow_after = self.after(120, self._animate_glow)

    def update_model(self, model: WorkerModel, *, selected: bool | None = None) -> None:
        self.model = model
        if selected is not None:
            self.selected = selected
        self.order_text.set(str(model.order + 1))
        self.title_text.set(model.title or "ChatGPT")
        self.page_text.set(
            f"page {model.page_id[:8]}  •  {model.browser_state}"
            + ("  •  login required" if model.requires_login else "")
        )
        self.role_var.set(model.role or "")
        self.enabled_var.set(model.enabled)
        self.status_text.set(model.status.value.upper())
        status_color = STATUS_COLORS.get(model.status.value, COLORS["muted"])
        self.status_label.configure(fg=status_color)
        preview = " ".join(model.prompt_template.split())
        self.prompt_text.set(preview[:220] + ("…" if len(preview) > 220 else ""))
        active = model.status in {
            WorkerStatus.PREPARING,
            WorkerStatus.RUNNING,
            WorkerStatus.WAITING,
            WorkerStatus.STOPPING,
        }
        self._set_glow(active)
        self.stop_button.state(["!disabled"] if active or model.status is WorkerStatus.QUEUED else ["disabled"])

    def destroy(self) -> None:
        if self._glow_after is not None:
            self.after_cancel(self._glow_after)
            self._glow_after = None
        if self._tick_after is not None:
            self.after_cancel(self._tick_after)
            self._tick_after = None
        super().destroy()


class ScrollableWorkerList(ttk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        *,
        on_select: PageCallback | None = None,
        on_role_change: RoleCallback | None = None,
        on_enabled: BoolCallback | None = None,
        on_edit_prompt: PageCallback | None = None,
        on_view_response: PageCallback | None = None,
        on_stop: PageCallback | None = None,
        on_release: PageCallback | None = None,
        on_reorder: ReorderCallback | None = None,
    ) -> None:
        super().__init__(master, style="Panel.TFrame")
        self.on_select = on_select
        self.on_role_change = on_role_change
        self.on_enabled = on_enabled
        self.on_edit_prompt = on_edit_prompt
        self.on_view_response = on_view_response
        self.on_stop = on_stop
        self.on_release = on_release
        self.on_reorder = on_reorder
        self.cards: dict[str, WorkerCard] = {}
        self.selected_page_id: str | None = None
        self._drag_page_id: str | None = None

        self.canvas = tk.Canvas(
            self,
            bg=COLORS["panel"],
            highlightthickness=0,
            bd=0,
        )
        self.scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.canvas.yview,
            style="Studio.Vertical.TScrollbar",
        )
        self.inner = tk.Frame(self.canvas, bg=COLORS["panel"])
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._sync_scrollregion)
        self.canvas.bind("<Configure>", self._sync_width)
        self.canvas.bind_all("<MouseWheel>", self._mousewheel, add="+")

    def _sync_scrollregion(self, _event: tk.Event | None = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_width(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.window, width=event.width)

    def _mousewheel(self, event: tk.Event) -> None:
        if self.winfo_containing(event.x_root, event.y_root) is not None:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _select(self, page_id: str) -> None:
        self.selected_page_id = page_id
        for key, card in self.cards.items():
            card.update_model(card.model, selected=key == page_id)
        if self.on_select is not None:
            self.on_select(page_id)

    def _drag_start(self, page_id: str, _root_y: int) -> None:
        self._drag_page_id = page_id
        self._select(page_id)

    def _drag_end(self, page_id: str, root_y: int) -> None:
        if self._drag_page_id != page_id:
            return
        ordered = sorted(self.cards.values(), key=lambda card: card.model.order)
        bounds = [(card.winfo_rooty(), card.winfo_rooty() + card.winfo_height()) for card in ordered]
        target = drop_target_index(bounds, root_y)
        self._drag_page_id = None
        if self.on_reorder is not None:
            self.on_reorder(page_id, target)

    def set_workers(self, workers: Sequence[WorkerModel]) -> None:
        order = [item.page_id for item in sorted(workers, key=lambda item: item.order)]
        if order != [card.model.page_id for card in sorted(self.cards.values(), key=lambda card: card.model.order)]:
            for card in self.cards.values():
                card.destroy()
            self.cards.clear()
            for item in sorted(workers, key=lambda worker: worker.order):
                card = WorkerCard(
                    self.inner,
                    model=item,
                    selected=item.page_id == self.selected_page_id,
                    on_select=self._select,
                    on_role_change=self.on_role_change,
                    on_enabled=self.on_enabled,
                    on_edit_prompt=self.on_edit_prompt,
                    on_view_response=self.on_view_response,
                    on_stop=self.on_stop,
                    on_release=self.on_release,
                    on_drag_start=self._drag_start,
                    on_drag_end=self._drag_end,
                )
                card.pack(fill="x", padx=8, pady=(0, 8))
                self.cards[item.page_id] = card
        else:
            by_id = {item.page_id: item for item in workers}
            for page_id, card in self.cards.items():
                card.update_model(
                    by_id[page_id],
                    selected=page_id == self.selected_page_id,
                )
        self._sync_scrollregion()


class LogNotebook(ttk.Notebook):
    def __init__(self, master: tk.Misc, *, max_lines: int = 2_000) -> None:
        super().__init__(master, style="Studio.TNotebook")
        self.max_lines = max_lines
        self.channels: dict[str, scrolledtext.ScrolledText] = {}
        self._create_channel("All")
        self._create_channel("System")

    def _create_channel(self, channel: str) -> scrolledtext.ScrolledText:
        frame = tk.Frame(self, bg=COLORS["panel"])
        text = scrolledtext.ScrolledText(
            frame,
            wrap="word",
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            selectbackground=COLORS["selection"],
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=10,
            font=("Consolas", 9),
        )
        text.pack(fill="both", expand=True)
        text.configure(state="disabled")
        self.add(frame, text=channel)
        self.channels[channel] = text
        return text

    def ensure_channel(self, channel: str) -> None:
        if channel and channel not in self.channels:
            self._create_channel(channel)

    def append(self, channel: str, message: str, *, mirror_all: bool = True) -> None:
        channel = channel or "System"
        self.ensure_channel(channel)
        self._append_to(self.channels[channel], message)
        if mirror_all and channel != "All":
            self._append_to(self.channels["All"], f"[{channel}] {message}")

    def _append_to(self, widget: scrolledtext.ScrolledText, message: str) -> None:
        widget.configure(state="normal")
        widget.insert("end", message.rstrip() + "\n")
        line_count = int(widget.index("end-1c").split(".")[0])
        if line_count > self.max_lines:
            widget.delete("1.0", f"{line_count - self.max_lines + 1}.0")
        widget.see("end")
        widget.configure(state="disabled")

    def get_text(self, channel: str) -> str:
        widget = self.channels[channel]
        return widget.get("1.0", "end-1c")


class ResponseDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, worker: WorkerModel) -> None:
        super().__init__(master)
        self.title(f"Responses · {worker.display_role}")
        self.geometry("900x650")
        self.configure(bg=COLORS["bg"])
        notebook = ttk.Notebook(self, style="Studio.TNotebook")
        notebook.pack(fill="both", expand=True, padx=12, pady=12)
        self._add_text(notebook, "Latest", worker.latest_response or "No response yet.")
        history = "\n\n".join(
            f"--- Response {index + 1} ---\n{text}"
            for index, text in enumerate(worker.responses)
        ) or "No response history yet."
        self._add_text(notebook, "History", history)
        self._add_text(notebook, "Last Prompt", worker.last_prompt or "No prompt sent yet.")
        ttk.Button(self, text="Close", style="Quiet.TButton", command=self.destroy).pack(
            pady=(0, 12)
        )
        self.transient(master.winfo_toplevel())
        self.grab_set()

    @staticmethod
    def _add_text(notebook: ttk.Notebook, title: str, content: str) -> None:
        frame = tk.Frame(notebook, bg=COLORS["panel"])
        text = scrolledtext.ScrolledText(
            frame,
            wrap="word",
            bg=COLORS["input"],
            fg=COLORS["text"],
            relief="flat",
            padx=12,
            pady=12,
            font=("Consolas", 10),
        )
        text.insert("1.0", content)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)
        notebook.add(frame, text=title)


class PromptDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        worker: WorkerModel,
        on_save: Callable[[str, bool], None],
    ) -> None:
        super().__init__(master)
        self.title(f"Prompt · {worker.display_role}")
        self.geometry("760x500")
        self.configure(bg=COLORS["bg"])
        self.on_save = on_save
        tk.Label(
            self,
            text="Role-specific instruction",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", padx=14, pady=(14, 6))
        self.text = scrolledtext.ScrolledText(
            self,
            wrap="word",
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            padx=12,
            pady=12,
            font=("Segoe UI", 10),
        )
        self.text.insert("1.0", worker.prompt_template)
        self.text.pack(fill="both", expand=True, padx=14)
        self.retry_var = tk.BooleanVar(value=False)
        active = worker.status in {
            WorkerStatus.PREPARING,
            WorkerStatus.RUNNING,
            WorkerStatus.WAITING,
            WorkerStatus.STOPPING,
        }
        retry = ttk.Checkbutton(
            self,
            text="Stop current response and retry this worker with the edited prompt",
            variable=self.retry_var,
            style="Studio.TCheckbutton",
        )
        retry.pack(anchor="w", padx=14, pady=10)
        if not active:
            retry.state(["disabled"])
        buttons = tk.Frame(self, bg=COLORS["bg"])
        buttons.pack(fill="x", padx=14, pady=(0, 14))
        ttk.Button(buttons, text="Cancel", style="Quiet.TButton", command=self.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self._save).pack(
            side="right", padx=(0, 8)
        )
        self.transient(master.winfo_toplevel())
        self.grab_set()
        self.text.focus_set()

    def _save(self) -> None:
        value = self.text.get("1.0", "end-1c").strip()
        if not value:
            return
        self.on_save(value, bool(self.retry_var.get()))
        self.destroy()
